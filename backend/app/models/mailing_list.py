"""
MailingList: a named group of email recipients and the sites they care
about, part of the Sites & Mailing Lists subsystem (group-of-four
extension).

The site <-> list relationship is many-to-many and is stored on this side
only: a list holds the ids of the sites it covers (site_ids). A Site never
stores which lists cover it, so there is exactly one place to keep in sync.

This class stores site IDS, not Site objects, and does not check that
those ids exist - it has no access to the site directory. Cross-reference
validation (a list pointing at a site id that does not exist) is the job of
SiteDirectory and its seed loaders (app/sites.py), which do have that access.

Seed data: one line of data/mailing_lists.jsonl looks like
    {"list_id": "ops-team", "name": "Operations team",
     "members": ["ops1@example.com", "ops2@example.com"], "site_ids": [1042, 1043]}
and is turned into a MailingList by MailingList.from_dict().
Only synthetic addresses (e.g. @example.com) belong in seed data.
"""

import re
from datetime import datetime, timezone
from typing import FrozenSet, Iterable, Optional, Tuple

from app.models._permissions import require_admin
from app.models.user import User

_LIST_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_LIST_ID_LENGTH = 50
_MAX_NAME_LENGTH = 100
# Deliberately simple: something@something.tld with no spaces. Full RFC 5322
# validation is out of scope; real delivery is not attempted (see the
# Notifier in a later step).
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MailingList:
    """
    A mailing list: list_id (read-only slug such as "ops-team"), a display
    name, a set of member email addresses, and a set of related site ids.

    Members and site ids are stored as sets - a recipient or a site can only
    be on a list once, and order has no meaning. The read-only `members` and
    `site_ids` properties expose them as sorted tuples so output is stable
    (a set's own iteration order must never be relied on).

    Email addresses are trimmed and lower-cased before they are stored, so
    "Ops1@Example.com" and "ops1@example.com" count as the same member.

    Removing a member and unlinking a site deliberately behave differently
    (this is the set.remove vs set.discard distinction):
      - remove_member() uses set.remove: removing someone who is not on the
        list is a mistake by the caller, so it raises ValueError.
      - unlink_site() uses set.discard: unlinking a site that is not linked
        is a normal, harmless case (archiving a site unlinks it from every
        list, whether or not each list covered it), so it is a silent no-op.

    Like Site, a list can be archived (soft delete) and restored by an admin.
    """

    def __init__(self, list_id: str, name: str, members: Iterable[str] = (),
                 site_ids: Iterable[int] = ()):
        self._list_id = self.validate_list_id(list_id)
        self.name = name  # validated by the property setter
        self._members: set = set()
        self._site_ids: set = set()
        for email in members:
            self.add_member(email)
        for site_id in site_ids:
            self.link_site(site_id)
        self.created_at = datetime.now(timezone.utc)
        self.archived_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Alternate constructor
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> "MailingList":
        """
        Build a MailingList from one parsed line of data/mailing_lists.jsonl.

        Required keys: list_id (str), name (str). Optional keys: members
        (list of email strings), site_ids (list of positive ints). Other keys
        are ignored. Whether each site id actually exists is NOT checked here
        (see the module docstring).

        Raises:
            ValueError: if `data` is not a dict, a required key is missing,
                members/site_ids is not a list, or any value is invalid. The
                message names the record's list_id when available.
        """
        if not isinstance(data, dict):
            raise ValueError(f"a mailing list record must be a JSON object, got {type(data).__name__}")
        ref = data.get("list_id", "?")
        for key in ("list_id", "name"):
            if key not in data:
                raise ValueError(f"mailing list record {ref!r} is missing required field '{key}'")
        members = data.get("members", [])
        site_ids = data.get("site_ids", [])
        for key, value in (("members", members), ("site_ids", site_ids)):
            if not isinstance(value, list):
                raise ValueError(f"mailing list record {ref!r}: '{key}' must be a list")
        try:
            return cls(list_id=data["list_id"], name=data["name"], members=members, site_ids=site_ids)
        except ValueError as exc:
            raise ValueError(f"mailing list record {ref!r}: {exc}") from None

    @classmethod
    def from_persisted(cls, list_id: str, name: str, members: Iterable[str], site_ids: Iterable[int],
                       created_at: datetime, archived_at: Optional[datetime] = None) -> "MailingList":
        """
        Rebuild a MailingList exactly as it was stored - used ONLY by the
        site store (app/persistence_sites.py) at startup. Values still go
        through normal validation; only the timestamps and archive state are
        restored directly (archive() would stamp a new time and check
        permissions, which make no sense when reloading saved data).
        """
        mailing_list = cls(list_id=list_id, name=name, members=members, site_ids=site_ids)
        mailing_list.created_at = created_at
        mailing_list.archived_at = archived_at
        return mailing_list

    # ------------------------------------------------------------------
    # Static validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def validate_list_id(list_id) -> str:
        """
        Return `list_id` if it is a lower-case slug (letters, digits and
        single hyphens, e.g. "ops-team"), at most 50 characters; raise
        ValueError otherwise.
        """
        if not isinstance(list_id, str) or not _LIST_ID_PATTERN.match(list_id) \
                or len(list_id) > _MAX_LIST_ID_LENGTH:
            raise ValueError(
                f"list_id must be lower-case letters, digits and single hyphens "
                f"(max {_MAX_LIST_ID_LENGTH} characters), got {list_id!r}"
            )
        return list_id

    @staticmethod
    def normalize_email(email) -> str:
        """Return the trimmed, lower-cased address if it looks like an email; raise ValueError otherwise."""
        if not isinstance(email, str) or not _EMAIL_PATTERN.match(email.strip()):
            raise ValueError(f"invalid email address: {email!r}")
        return email.strip().lower()

    @staticmethod
    def _validate_site_id(site_id) -> int:
        """Same rule as Site.validate_site_id (positive int, bool rejected), kept local to avoid importing Site."""
        if isinstance(site_id, bool) or not isinstance(site_id, int) or site_id <= 0:
            raise ValueError(f"site ids must be positive whole numbers, got {site_id!r}")
        return site_id

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def list_id(self) -> str:
        """Read-only: a list's id never changes after creation."""
        return self._list_id

    @property
    def name(self) -> str:
        return self._name

    @name.setter
    def name(self, value) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("A mailing list name is required.")
        value = value.strip()
        if len(value) > _MAX_NAME_LENGTH:
            raise ValueError(f"A mailing list name must be at most {_MAX_NAME_LENGTH} characters.")
        self._name = value

    @property
    def members(self) -> Tuple[str, ...]:
        """Member addresses, sorted alphabetically."""
        return tuple(sorted(self._members))

    @property
    def recipients(self) -> FrozenSet[str]:
        """Member addresses as an immutable set - what a Notification combines across lists (set union)."""
        return frozenset(self._members)

    @property
    def site_ids(self) -> Tuple[int, ...]:
        """Related site ids, sorted ascending."""
        return tuple(sorted(self._site_ids))

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    # ------------------------------------------------------------------
    # Collection operations
    # ------------------------------------------------------------------

    def add_member(self, email: str) -> bool:
        """Add a member. Returns True if added, False if already a member. Raises ValueError for an invalid address."""
        email = self.normalize_email(email)
        if email in self._members:
            return False
        self._members.add(email)
        return True

    def remove_member(self, email: str) -> None:
        """
        Remove a member (set.remove semantics). Raises ValueError if the
        address is invalid or is not on this list - see the class docstring.
        """
        email = self.normalize_email(email)
        try:
            self._members.remove(email)
        except KeyError:
            raise ValueError(f"{email} is not a member of {self._list_id!r}.") from None

    def link_site(self, site_id: int) -> bool:
        """Relate a site to this list. Returns True if newly linked, False if it already was."""
        site_id = self._validate_site_id(site_id)
        if site_id in self._site_ids:
            return False
        self._site_ids.add(site_id)
        return True

    def unlink_site(self, site_id: int) -> None:
        """Remove a site from this list (set.discard semantics: a no-op if it was not linked)."""
        self._site_ids.discard(site_id)

    def covers(self, site_id: int) -> bool:
        """True if this list is related to the given site."""
        return site_id in self._site_ids

    # ------------------------------------------------------------------
    # Archive / restore
    # ------------------------------------------------------------------

    def archive(self, actor: User) -> None:
        """
        Soft-delete the list (admin only). Its members and site links are
        kept, so restoring it brings it back exactly as it was.

        Raises:
            PermissionError: if actor is not an admin.
            RuntimeError: if the list is already archived.
        """
        require_admin(actor, "archive a mailing list")
        if self.is_archived:
            raise RuntimeError(f"Mailing list {self._list_id!r} is already archived.")
        self.archived_at = datetime.now(timezone.utc)

    def restore(self, actor: User) -> None:
        """
        Bring an archived list back (admin only), with its members and site
        links unchanged. No site reconciliation is needed: archiving a site
        unlinks it from EVERY list, archived lists included (see
        SiteDirectory.archive_site() in app/sites.py), so a list never holds
        a link to an archived site.

        Raises:
            PermissionError: if actor is not an admin.
            RuntimeError: if the list is not archived.
        """
        require_admin(actor, "restore a mailing list")
        if not self.is_archived:
            raise RuntimeError(f"Mailing list {self._list_id!r} is not archived.")
        self.archived_at = None

    # ------------------------------------------------------------------
    # Dunder methods
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        """Number of members."""
        return len(self._members)

    def __contains__(self, email) -> bool:
        """`"a@example.com" in mailing_list` - membership test (case-insensitive, invalid input is simply False)."""
        try:
            return self.normalize_email(email) in self._members
        except ValueError:
            return False

    def __str__(self) -> str:
        archived = " (archived)" if self.is_archived else ""
        return (f"[MailingList {self._list_id}] {self._name} - "
                f"{len(self._members)} members, {len(self._site_ids)} sites{archived}")

    def __repr__(self) -> str:
        return (f"MailingList(list_id={self._list_id!r}, name={self._name!r}, "
                f"members={len(self._members)}, site_ids={self.site_ids!r}, archived={self.is_archived})")
