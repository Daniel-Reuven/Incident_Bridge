"""
Site: one organization site (a website / web service the organization
runs), part of the Sites & Mailing Lists subsystem - the group-of-four
extension.

A Site owns its own status, its status history, and the result of its
latest availability check. It does NOT know which mailing lists cover it
(MailingList holds that link - see mailing_list.py), does not run
availability checks itself (the checker service does -
app/services/availability.py - and hands the result to record_check()),
and does not touch any storage
(persistence is a separate store - app/persistence_sites.py). Keeping those
concerns out of this class is what lets each of them change or be swapped
independently.

Seed data: one line of data/sites.jsonl looks like
    {"site_id": 1042, "site_name": "Customer portal",
     "site_url": "https://example.com", "site_publish_date": "2025-03-01"}
and is turned into a Site by Site.from_dict().

Incidents refer to a site in free text as "Site <site_id>", e.g.
"Site 1042" - see the `label` property below.
"""

from datetime import date, datetime, timezone
from typing import FrozenSet, Iterable, NamedTuple, Optional, Tuple
from urllib.parse import urlparse

from app.models._permissions import require_admin
from app.models.enums import SiteStatus
from app.models.user import User

# Who a status change is attributed to when no person made it (an
# automated availability check).
SYSTEM_ACTOR = "system"

_MAX_NAME_LENGTH = 100
_MAX_URL_LENGTH = 2048
_ALLOWED_URL_SCHEMES = ("http", "https")

# How many failed checks IN A ROW it takes to mark a site Down. The first
# failure only makes it Degraded ("suspect"), so one dropped request or a
# brief network blip never raises a full outage on its own.
FAILURES_BEFORE_DOWN = 2


class SiteStatusChange(NamedTuple):
    """
    One entry in a Site's status history - a short, fixed, immutable record,
    which is why it is a NamedTuple (a tuple with named fields) rather than a
    full class.

    `actor` is a username string (or SYSTEM_ACTOR for automated checks)
    rather than a User object, so a history entry stays meaningful even if
    that user is later removed from INCIDENT_BRIDGE_USERS.
    """
    old_status: SiteStatus
    new_status: SiteStatus
    reason: str
    actor: str
    changed_at: datetime


class AvailabilityCheck(NamedTuple):
    """
    The result of ONE availability check of a site's URL, produced by an
    AvailabilityChecker (app/services/availability.py) and recorded on the
    site with Site.record_check(). A short, fixed, immutable record - hence a
    NamedTuple.

    observed_status is what this single check saw (OPERATIONAL, DEGRADED or
    DOWN). It is not necessarily the site's new status: record_check()
    applies the "FAILURES_BEFORE_DOWN failures in a row" rule on top of it.
    http_status / response_ms are None when no HTTP response arrived at all;
    error then explains why (timeout, DNS failure, refused connection...).
    """
    checked_at: datetime
    observed_status: SiteStatus
    http_status: Optional[int] = None
    response_ms: Optional[float] = None
    error: Optional[str] = None

    @property
    def summary(self) -> str:
        """One readable line, e.g. 'HTTP 200 in 123 ms' or 'no response: timed out after 5s'."""
        if self.http_status is None:
            return f"no response: {self.error or 'unknown error'}"
        timing = f" in {self.response_ms:.0f} ms" if self.response_ms is not None else ""
        return f"HTTP {self.http_status}{timing}"


class Site:
    """
    An organization site with a validated identity (id, name, URL, publish
    date) and a status lifecycle:

        UNKNOWN -> OPERATIONAL / DEGRADED / DOWN / MAINTENANCE -> ...

    Every status change - manual or automated - is appended to the status
    history (see change_status() and record_check()). A site can be ARCHIVED (soft delete) and
    later RESTORED; while archived it cannot change status, and the API
    layer excludes it from checks, notifications and the report.

    Validation: site_name, site_url and site_publish_date are properties
    whose setters raise ValueError on invalid input, so a Site can never be
    left holding an invalid value, whether it was built by the constructor,
    by from_dict(), or edited later. site_id is read-only after creation:
    ids are never reused or changed, so old incident text that says
    "Site 1042" always points at the same site.

    Equality and hashing are by site_id (two Site objects with the same id
    are the same site), which lets sites be used safely in sets and as dict
    keys.
    """

    def __init__(self, site_id: int, site_name: str, site_url: str,
                 site_publish_date: Optional[date] = None):
        self._site_id = self.validate_site_id(site_id)
        self.site_name = site_name                  # validated by the property setter
        self.site_url = site_url                    # validated by the property setter
        self.site_publish_date = site_publish_date  # validated by the property setter
        self._status = SiteStatus.UNKNOWN
        self._history: list = []
        self.created_at = datetime.now(timezone.utc)
        self.archived_at: Optional[datetime] = None
        # The mailing lists this site belonged to when it was archived, so
        # restore() can tell the caller which links to bring back.
        self._archived_list_ids: FrozenSet[str] = frozenset()
        # Availability-check state (see record_check()).
        self._last_check: Optional[AvailabilityCheck] = None
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Alternate constructor
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> "Site":
        """
        Build a Site from one parsed line of data/sites.jsonl.

        Required keys: site_id (int), site_name (str), site_url (str).
        Optional key: site_publish_date (an ISO date string, "YYYY-MM-DD",
        or null). Any other keys are ignored - system-managed fields
        (status, history, archive state) are never taken from seed data.

        Raises:
            ValueError: if `data` is not a dict, a required key is missing,
                or any value is invalid (the message names the field and,
                when available, the record's site_id).
        """
        if not isinstance(data, dict):
            raise ValueError(f"a site record must be a JSON object, got {type(data).__name__}")
        ref = data.get("site_id", "?")
        for key in ("site_id", "site_name", "site_url"):
            if key not in data:
                raise ValueError(f"site record {ref!r} is missing required field '{key}'")
        publish_date = cls.parse_publish_date(data.get("site_publish_date"))
        try:
            return cls(site_id=data["site_id"], site_name=data["site_name"],
                       site_url=data["site_url"], site_publish_date=publish_date)
        except ValueError as exc:
            raise ValueError(f"site record {ref!r}: {exc}") from None

    @classmethod
    def from_persisted(cls, site_id: int, site_name: str, site_url: str, site_publish_date: Optional[date],
                       status: SiteStatus, history: Iterable[SiteStatusChange], created_at: datetime,
                       archived_at: Optional[datetime] = None,
                       archived_list_ids: Iterable[str] = (),
                       last_check: Optional[AvailabilityCheck] = None,
                       consecutive_failures: int = 0) -> "Site":
        """
        Rebuild a Site exactly as it was stored - used ONLY by the site store
        (app/persistence_sites.py) at startup. The identity fields still go
        through the normal validation (so a corrupted row fails loudly), but
        the system-managed state (status, history, timestamps, archive state)
        is restored as-is rather than replayed through change_status(),
        record_check() and archive(), which would add new history entries and
        new timestamps.
        """
        site = cls(site_id=site_id, site_name=site_name, site_url=site_url, site_publish_date=site_publish_date)
        site._status = status
        site._history = list(history)
        site.created_at = created_at
        site.archived_at = archived_at
        site._archived_list_ids = frozenset(archived_list_ids)
        site._last_check = last_check
        site._consecutive_failures = consecutive_failures
        return site

    # ------------------------------------------------------------------
    # Static validation helpers (shared by the setters and from_dict)
    # ------------------------------------------------------------------

    @staticmethod
    def validate_site_id(site_id) -> int:
        """Return `site_id` if it is a positive int; raise ValueError otherwise (bool is rejected too)."""
        if isinstance(site_id, bool) or not isinstance(site_id, int) or site_id <= 0:
            raise ValueError(f"site_id must be a positive whole number, got {site_id!r}")
        return site_id

    @staticmethod
    def validate_url(url) -> str:
        """
        Return the trimmed URL if it is an absolute http:// or https:// URL
        with a host; raise ValueError otherwise. Only http(s) is allowed
        because the server will later fetch this URL to check availability -
        other schemes (file://, ftp://, ...) must never reach that code.
        """
        if not isinstance(url, str) or not url.strip():
            raise ValueError("site_url is required")
        url = url.strip()
        if len(url) > _MAX_URL_LENGTH:
            raise ValueError(f"site_url must be at most {_MAX_URL_LENGTH} characters")
        parsed = urlparse(url)
        if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES or not parsed.netloc:
            raise ValueError(f"site_url must start with http:// or https:// and include a host, got {url!r}")
        return url

    @staticmethod
    def parse_publish_date(value) -> Optional[date]:
        """
        Convert a seed/API value into a date: None stays None, a date passes
        through, an ISO "YYYY-MM-DD" string is parsed. Anything else raises
        ValueError. (The not-in-the-future rule is checked by the setter.)
        """
        if value is None or isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value.strip())
            except ValueError:
                raise ValueError(f"site_publish_date must be a date in YYYY-MM-DD format, got {value!r}") from None
        raise ValueError(f"site_publish_date must be a date in YYYY-MM-DD format, got {value!r}")

    # ------------------------------------------------------------------
    # Validated properties
    # ------------------------------------------------------------------

    @property
    def site_id(self) -> int:
        """Read-only: a site's id never changes after creation."""
        return self._site_id

    @property
    def site_name(self) -> str:
        return self._site_name

    @site_name.setter
    def site_name(self, value) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("site_name is required")
        value = value.strip()
        if len(value) > _MAX_NAME_LENGTH:
            raise ValueError(f"site_name must be at most {_MAX_NAME_LENGTH} characters")
        self._site_name = value

    @property
    def site_url(self) -> str:
        return self._site_url

    @site_url.setter
    def site_url(self, value) -> None:
        self._site_url = self.validate_url(value)

    @property
    def site_publish_date(self) -> Optional[date]:
        return self._site_publish_date

    @site_publish_date.setter
    def site_publish_date(self, value) -> None:
        value = self.parse_publish_date(value)
        if value is not None and value > date.today():
            raise ValueError(f"site_publish_date cannot be in the future, got {value.isoformat()}")
        self._site_publish_date = value

    # ------------------------------------------------------------------
    # Computed, read-only properties
    # ------------------------------------------------------------------

    @property
    def status(self) -> SiteStatus:
        """Current status. Read-only: change it through change_status() so every change is recorded."""
        return self._status

    @property
    def status_history(self) -> Tuple[SiteStatusChange, ...]:
        """Every status change so far, oldest first, as an immutable tuple (callers can't edit the history)."""
        return tuple(self._history)

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @property
    def archived_list_ids(self) -> FrozenSet[str]:
        """Ids of the mailing lists this site was on when archived (empty while active)."""
        return self._archived_list_ids

    @property
    def last_check(self) -> Optional[AvailabilityCheck]:
        """The most recent availability check, or None if the site was never checked."""
        return self._last_check

    @property
    def consecutive_failures(self) -> int:
        """How many checks in a row have observed the site as Down (0 after any successful check)."""
        return self._consecutive_failures

    @property
    def label(self) -> str:
        """How incidents refer to this site in free text, e.g. 'Site 1042'."""
        return f"Site {self._site_id}"

    # ------------------------------------------------------------------
    # Business operations
    # ------------------------------------------------------------------

    def change_status(self, new_status: SiteStatus, reason: str,
                      actor: Optional[User] = None) -> Optional[SiteStatusChange]:
        """
        Move the site to `new_status` and append the change to its history.

        `actor` is the admin making a manual change, or None for an
        automated change (recorded as SYSTEM_ACTOR). A manual change requires
        an admin, and also resets the automated failure count
        (consecutive_failures) - an admin override starts afresh.
        MAINTENANCE can only be set manually. Automated CHECKS normally go
        through record_check(), which applies the failure rules first.

        Returns the new SiteStatusChange, or None if `new_status` equals the
        current status - repeated checks that find the same result must not
        flood the history (and, in a later step, must not create a new
        notification each time).

        Checks run in this order (nothing changes if any fails): input
        (status type, reason), permission, then current state.

        Raises:
            ValueError: if new_status is not a SiteStatus, is UNKNOWN (only
                the system sets that, on creation/restore), reason is blank,
                or an automated change tries to set MAINTENANCE.
            PermissionError: if `actor` is given and is not an admin.
            RuntimeError: if the site is archived (the API layer maps this
                to HTTP 409).
        """
        if not isinstance(new_status, SiteStatus):
            raise ValueError(f"new_status must be a SiteStatus, got {new_status!r}")
        if new_status == SiteStatus.UNKNOWN:
            raise ValueError("A site cannot be set to Unknown - that status only marks a site not yet checked.")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A reason is required to change a site's status.")
        if actor is None and new_status == SiteStatus.MAINTENANCE:
            raise ValueError("Maintenance can only be set manually by an admin.")
        if actor is not None:
            require_admin(actor, "change a site's status")
        if self.is_archived:
            raise RuntimeError(f"{self.label} is archived - restore it before changing its status.")

        if actor is not None:
            # A manual override starts the automated failure count afresh.
            self._consecutive_failures = 0
        if new_status == self._status:
            return None
        return self._record(new_status, reason.strip(), actor.username if actor else SYSTEM_ACTOR)

    def record_check(self, check: AvailabilityCheck) -> Optional[SiteStatusChange]:
        """
        Record the result of an automated availability check and move the
        site to the status it implies. This is where the check process's
        state rules live:

          - the check is always stored as last_check;
          - observed DOWN: consecutive_failures goes up by one. Below
            FAILURES_BEFORE_DOWN the site becomes DEGRADED (suspect); at or
            above it, DOWN;
          - observed OPERATIONAL / DEGRADED: consecutive_failures resets to 0
            and the site takes the observed status;
          - a site in MAINTENANCE keeps its status (only an admin ends
            maintenance) - the check is stored, but nothing else changes and
            failures are not counted.

        The status change, if any, is recorded like any other (actor
        "system", reason built from the check, e.g. "Automated check: HTTP
        503 in 120 ms (failure 2 in a row)").

        Returns the SiteStatusChange, or None if the status did not change.

        Raises:
            ValueError: if `check` is not an AvailabilityCheck, or its
                observed status is not Operational / Degraded / Down.
            RuntimeError: if the site is archived (archived sites are not
                checked - the API layer maps this to HTTP 409).
        """
        if not isinstance(check, AvailabilityCheck):
            raise ValueError(f"check must be an AvailabilityCheck, got {check!r}")
        if check.observed_status not in (SiteStatus.OPERATIONAL, SiteStatus.DEGRADED, SiteStatus.DOWN):
            raise ValueError(f"A check can only observe Operational, Degraded or Down, got {check.observed_status!r}")
        if self.is_archived:
            raise RuntimeError(f"{self.label} is archived - archived sites are not checked.")

        self._last_check = check
        if self._status == SiteStatus.MAINTENANCE:
            return None

        reason = f"Automated check: {check.summary}"
        if check.observed_status == SiteStatus.DOWN:
            self._consecutive_failures += 1
            target = SiteStatus.DOWN if self._consecutive_failures >= FAILURES_BEFORE_DOWN else SiteStatus.DEGRADED
            reason += f" (failure {self._consecutive_failures} in a row)"
        else:
            self._consecutive_failures = 0
            target = check.observed_status

        if target == self._status:
            return None
        return self._record(target, reason, SYSTEM_ACTOR)

    def archive(self, actor: User, list_ids: Iterable[str] = ()) -> None:
        """
        Soft-delete the site (admin only). `list_ids` are the mailing lists
        it is on right now; they are remembered so restore() can report
        which links to bring back. Unlinking the site from those lists is
        the caller's job (SiteDirectory.archive_site() in app/sites.py) -
        this method only changes the site itself.

        Raises:
            PermissionError: if actor is not an admin.
            RuntimeError: if the site is already archived.
        """
        require_admin(actor, "archive a site")
        if self.is_archived:
            raise RuntimeError(f"{self.label} is already archived.")
        self._archived_list_ids = frozenset(list_ids)
        self.archived_at = datetime.now(timezone.utc)

    def restore(self, actor: User) -> FrozenSet[str]:
        """
        Bring an archived site back (admin only). Its status returns to
        UNKNOWN (its real state is unknown until the next check), the
        failure streak resets, and the reset is recorded in the history with
        reason "Restored from archive". The last check result is kept, for
        reference only.

        Returns the ids of the mailing lists it was on when archived; the
        caller re-links it to the ones that still exist and are active.

        Raises:
            PermissionError: if actor is not an admin.
            RuntimeError: if the site is not archived.
        """
        require_admin(actor, "restore a site")
        if not self.is_archived:
            raise RuntimeError(f"{self.label} is not archived.")
        list_ids = self._archived_list_ids
        self.archived_at = None
        self._archived_list_ids = frozenset()
        self._consecutive_failures = 0   # the old failure streak says nothing about the site today
        if self._status != SiteStatus.UNKNOWN:
            self._record(SiteStatus.UNKNOWN, "Restored from archive", actor.username)
        return list_ids

    def _record(self, new_status: SiteStatus, reason: str, actor_name: str) -> SiteStatusChange:
        """Apply a status change and append it to the history (no validation - callers validate first)."""
        change = SiteStatusChange(self._status, new_status, reason, actor_name, datetime.now(timezone.utc))
        self._status = new_status
        self._history.append(change)
        return change

    # ------------------------------------------------------------------
    # Dunder methods
    # ------------------------------------------------------------------

    def __eq__(self, other) -> bool:
        if not isinstance(other, Site):
            return NotImplemented
        return self._site_id == other._site_id

    def __hash__(self) -> int:
        return hash(self._site_id)

    def __str__(self) -> str:
        archived = " (archived)" if self.is_archived else ""
        return f"[{self.label} - {self._status.name}] {self._site_name}{archived}"

    def __repr__(self) -> str:
        return (f"Site(site_id={self._site_id}, site_name={self._site_name!r}, "
                f"site_url={self._site_url!r}, status={self._status.name}, archived={self.is_archived})")
