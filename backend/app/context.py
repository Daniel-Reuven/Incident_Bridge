"""
Custom context managers (Stage 1 syllabus: context managers).

IncidentWorkSession - brackets a period of active work on an incident.
    There's no file or network resource to guard at this layer - the
    "resource" being protected is the incident's audit trail: whatever
    happens inside the `with` block (success or exception), the comment
    thread ends up with an accurate record of what occurred.

SiteCheckSession - brackets one availability check of a site (Sites &
    Mailing Lists subsystem). It temporarily LOCKS the site for checking -
    a second check of the same site while one is running is refused - and
    measures how long the check took. The lock is always released on exit,
    even when the check raises, so a crashed check can never leave a site
    locked forever.

Neither one ever suppresses an exception: __exit__ returns False.
"""

import threading
import time
from datetime import datetime, timezone
from typing import Optional, Set

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

    def __repr__(self) -> str:
        """Developer view: the incident's repr, the actor's username, and whether the session has started."""
        return (f"IncidentWorkSession(incident={self.incident!r}, actor={self.actor.username!r}, "
                f"started={self._start is not None})")


class SiteCheckSession:
    """
    Usage (see SiteDirectory.check_site() in app/sites.py):

        with SiteCheckSession(site) as session:
            check = checker.check(site.site_url)
            site.record_check(check)
        session.duration_ms   # how long the block took

    On enter: refuses to start (RuntimeError, mapped to HTTP 409 by the API
    layer) if the same site is already being checked - e.g. an admin
    pressing "Check" while a "Check all" is still working through that site.
    Otherwise marks the site as being checked and starts a timer.
    On exit (clean OR exception): un-marks the site and stores duration_ms.
    The exception, if any, propagates unchanged.

    The set of sites being checked is shared by every session in this
    process (a class attribute) and guarded by a lock, because FastAPI runs
    sync endpoints in a thread pool, so two requests really can try to check
    the same site at the same moment.
    """

    _active: Set[int] = set()
    _lock = threading.Lock()

    def __init__(self, site):
        self.site = site
        self.duration_ms: Optional[float] = None
        self._started: Optional[float] = None

    @classmethod
    def is_running(cls, site_id: int) -> bool:
        """True while a check of this site is in progress."""
        with cls._lock:
            return site_id in cls._active

    def __enter__(self) -> "SiteCheckSession":
        with self._lock:
            if self.site.site_id in self._active:
                raise RuntimeError(f"{self.site.label} is already being checked - wait for that check to finish.")
            self._active.add(self.site.site_id)
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.duration_ms = (time.perf_counter() - self._started) * 1000
        with self._lock:
            # discard, not remove: releasing must never fail, whatever happened inside the block.
            self._active.discard(self.site.site_id)
        return False  # never suppress the exception

    def __str__(self) -> str:
        state = "Finished" if self.duration_ms is not None else ("Running" if self._started else "Pending")
        return f"SiteCheckSession(site={self.site.label}, state={state})"

    def __repr__(self) -> str:
        return f"SiteCheckSession(site_id={self.site.site_id}, duration_ms={self.duration_ms!r})"
