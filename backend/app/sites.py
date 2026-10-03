"""
SiteDirectory: the in-memory home of every Site and MailingList, plus the
JSONL seed import that fills it at startup. Part of the Sites & Mailing
Lists subsystem (group-of-four extension).

Where this sits:
  - app/models/site.py, mailing_list.py, notification.py - the entities
    themselves (validation, status lifecycle, archive/restore of ONE object).
  - this module - the COLLECTION of them: lookup by id, duplicate rules,
    cross-reference rules between lists and sites, archive/restore
    cascades, saving every change, and loading seed files.
  - app/persistence_sites.py - the optional SQLite store behind it.
  - app/state.py - at startup: loads the stored data (bulk_load), THEN runs
    seed_site_directory(), so seed ids that are already stored are skipped.

Seed files (one JSON object per line):
    data/sites.jsonl          -> Site.from_dict()
    data/mailing_lists.jsonl  -> MailingList.from_dict(), and every site id it
                                 lists must exist in the directory
Sites are always loaded BEFORE mailing lists, because lists refer to sites.

Explicit decisions:
  - A seed record whose id (site_id, list_id) is ALREADY in the directory -
    normally because it was stored by an earlier run - is skipped and only
    counted ("already stored"), never overwritten, archived items included.
    That is the normal case on every restart, not a problem, and it is what
    makes the startup import safe: edits made in the app are never
    overwritten by the seed file, and an archived site is never brought
    back by it.
  - An id that appears TWICE in the same import is a real data problem: the
    second record is skipped and reported as a duplicate.
  - An invalid record (bad JSON, missing field, invalid value) is SKIPPED
    and reported with its line number; it never stops the rest of the file.
  - A mailing list that refers to a site id that does not exist (or is
    archived) is still loaded, WITHOUT that reference, and a warning is
    reported. Losing one link is less harmful than losing the whole list
    with all its members.
  - A missing seed file is not an error: the app starts with whatever loaded
    (possibly nothing) and prints a warning.
  - Invariant: no mailing list (active OR archived) ever links to an archived
    site. archive_site() unlinks the site from every list that covers it and
    remembers them; restore_site() re-links it to those lists. Because of
    that, restoring a list never needs to check its links.

Saving: every method here that changes something saves it through the
store (if one is configured). Code that changes a Site, MailingList or
Notification DIRECTLY (e.g. site.change_status(), list.add_member(),
editing a name) must call save_site() / save_mailing_list() /
save_notification() afterwards - the same rule as IncidentRepository.save().

Availability checks: check_site() / check_all() run the configured
AvailabilityChecker (app/services/availability.py) inside a
SiteCheckSession (app/context.py), record the result on the site
(Site.record_check(), which applies the failure rules) and save it.
Archived sites are never checked; check_all() also skips sites in
Maintenance, while check_site() still checks one on explicit request
(useful before ending maintenance) without changing its status.

Notification process: every status change - from a check (check_site) or
an admin (change_site_status) - that Notification.is_worth_notifying()
accepts creates a notification addressed to the ACTIVE lists covering the
site: a Draft, or Skipped when nobody would receive it. A new draft
supersedes any older unsent draft of the same site (no stale "Down" draft
left waiting after the site recovered). An admin then sends a draft
(send_notification - delivered through the configured Notifier,
app/services/notifier.py, and only marked Sent once delivery succeeded) or
dismisses it (dismiss_notification).

Admin create/edit operations (create_site, update_site,
create_mailing_list, update_mailing_list) are what the API's admin page
uses. They check the admin rule themselves (the same helper the models use),
so the rule holds no matter which caller reaches them. An edit is all or
nothing: if any field is invalid, nothing changes.

Site URLs are unique among ACTIVE sites (compared case-insensitively), so
the same address is never monitored - and notified about - twice. An
archived site's URL may be reused by a new site.

The incident scan and the site report live in their own modules
(app/site_scan.py, app/reports.py); scan_incidents() below is a thin
convenience wrapper that feeds this directory's site ids to the scan.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, NamedTuple, Optional, Tuple

from app.context import SiteCheckSession
from app.models import (
    AvailabilityCheck, MailingList, Notification, NotificationState, Site, SiteStatus, SiteStatusChange, User,
)
from app.persistence_sites import SqliteSiteStore
from app.services.availability import AvailabilityChecker
from app.models._permissions import require_admin
from app.services.notifier import Notifier
from app.site_scan import SiteScanResult, scan_incidents as _scan_incidents

# Ids given to sites created in the app (a later step) start here when the
# directory is empty, so every site reads like "Site 1001" in incident text.
FIRST_SITE_ID = 1001

# backend/app/sites.py -> parents[2] is the repository root, where data/ lives.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SITES_SEED = _REPO_ROOT / "data" / "sites.jsonl"
DEFAULT_MAILING_LISTS_SEED = _REPO_ROOT / "data" / "mailing_lists.jsonl"


@dataclass
class ImportReport:
    """
    Outcome of loading one seed file (or any iterable of JSONL lines).

    created_ids:           ids that were added to the directory.
    already_stored_ids:    ids that were already in the directory before this
                           import (e.g. stored by an earlier run) - skipped,
                           and NOT a problem.
    skipped_duplicate_ids: ids that appeared more than once in this import -
                           every repeat after the first was skipped.
    skipped_invalid:       one "line N: reason" entry per rejected record.
    warnings:              problems that did NOT stop a record from loading
                           (e.g. a mailing list's link to an unknown site
                           was dropped).
    """
    kind: str
    created_ids: list = field(default_factory=list)
    already_stored_ids: list = field(default_factory=list)
    skipped_duplicate_ids: list = field(default_factory=list)
    skipped_invalid: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def created(self) -> int:
        return len(self.created_ids)

    @property
    def problems(self) -> List[str]:
        """Every reportable problem as a readable line - what the startup log prints (already-stored ids are not problems)."""
        return ([f"duplicate id in file skipped: {dup!r}" for dup in self.skipped_duplicate_ids]
                + [f"invalid record skipped - {reason}" for reason in self.skipped_invalid]
                + [f"warning - {warning}" for warning in self.warnings])

    def __str__(self) -> str:
        return (f"{self.kind}: {self.created} loaded, {len(self.already_stored_ids)} already stored, "
                f"{len(self.skipped_duplicate_ids)} duplicates skipped, "
                f"{len(self.skipped_invalid)} invalid skipped, {len(self.warnings)} warnings")


class SiteCheckOutcome(NamedTuple):
    """
    What one check_site() call did: the check result, the status change it
    caused (None if the status stayed the same), how long it took, and the
    notification drafted for that change (None if there was no change, or
    the change was not worth notifying).
    """
    site_id: int
    check: AvailabilityCheck
    change: Optional[SiteStatusChange]
    duration_ms: float
    notification: Optional[Notification] = None


class StatusChangeOutcome(NamedTuple):
    """What change_site_status() did: the recorded change (None if the status was already that) and its notification."""
    site_id: int
    change: Optional[SiteStatusChange]
    notification: Optional[Notification] = None


def _parse_jsonl(lines: Iterable[str]) -> Iterator[Tuple[int, Optional[dict], Optional[str]]]:
    """
    Generator over JSONL text lines, one parsed record at a time (the file is
    never read into memory as a whole). Yields (line_number, record, error):
    `record` is the parsed value when the line is valid JSON, otherwise
    `error` explains why not. Blank lines are skipped silently. Whether the
    parsed value is a valid site/list is decided by the caller.
    """
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            yield line_number, json.loads(line), None
        except json.JSONDecodeError as exc:
            yield line_number, None, f"invalid JSON ({exc.msg})"


class SiteDirectory:
    """
    Holds every Site (by site_id), every MailingList (by list_id), archived
    ones included - archived items stay here so their ids can never be
    reused and so they can be restored - and every Notification (by id).

    Lookups raise KeyError (the API layer maps it to HTTP 404). Adding a
    duplicate id, or a list that points at a missing/archived site, raises
    ValueError (HTTP 400) - the strict rules for the app's own create
    operations. The seed loaders below catch those errors and turn them into
    report entries instead, so one bad record never blocks the rest.

    `store` is optional: without one (e.g. in unit tests or main.py) the
    directory is memory-only and every save is a no-op. `checker` is also
    optional: without one, check_site()/check_all() raise RuntimeError -
    app/state.py passes an HttpChecker, tests and the demo a FakeChecker.
    Likewise `notifier`: without one, send_notification() raises
    RuntimeError (drafts are still created) - app/state.py passes an
    OutboxNotifier.
    """

    def __init__(self, store: Optional[SqliteSiteStore] = None,
                 checker: Optional[AvailabilityChecker] = None,
                 notifier: Optional[Notifier] = None):
        self._sites: Dict[int, Site] = {}
        self._lists: Dict[str, MailingList] = {}
        self._notifications: Dict[str, Notification] = {}
        self._store = store
        self._checker = checker
        self._notifier = notifier

    # ------------------------------------------------------------------
    # Saving and startup loading
    # ------------------------------------------------------------------

    def save_site(self, site: Site) -> None:
        """Persist a site after any change to it (no-op without a store)."""
        if self._store:
            self._store.save_site(site)

    def save_mailing_list(self, mailing_list: MailingList) -> None:
        """Persist a mailing list after any change to it (no-op without a store)."""
        if self._store:
            self._store.save_mailing_list(mailing_list)

    def save_notification(self, notification: Notification) -> None:
        """Persist a notification after any change to it (no-op without a store)."""
        if self._store:
            self._store.save_notification(notification)

    def bulk_load(self, sites: Iterable[Site] = (), mailing_lists: Iterable[MailingList] = (),
                  notifications: Iterable[Notification] = ()) -> None:
        """
        Fill the directory with already-stored objects at startup (see
        app/state.py, which passes SqliteSiteStore.load_all()'s result).
        Does NOT save them again and does not re-check links - the data was
        valid when it was saved. Must run before seed_site_directory().
        """
        for site in sites:
            self._sites[site.site_id] = site
        for mailing_list in mailing_lists:
            self._lists[mailing_list.list_id] = mailing_list
        for notification in notifications:
            self._notifications[notification.id] = notification

    # ------------------------------------------------------------------
    # Adding (strict - used directly by the app, wrapped by the loaders)
    # ------------------------------------------------------------------

    def add_site(self, site: Site) -> None:
        """
        Add a site. Raises ValueError if its site_id is already used (active
        or archived), or if an active site already has the same URL.
        """
        if site.site_id in self._sites:
            raise ValueError(f"{site.label} already exists.")
        self._ensure_url_free(site.site_url)
        self._sites[site.site_id] = site
        self.save_site(site)

    def _ensure_url_free(self, url: str, ignore_site_id: Optional[int] = None) -> None:
        """Raise ValueError if an ACTIVE site (other than `ignore_site_id`) already uses this URL (case-insensitive)."""
        wanted = url.strip().lower()
        for other in self.sites():
            if other.site_id != ignore_site_id and other.site_url.lower() == wanted:
                raise ValueError(f"{other.label} ({other.site_name}) already uses {url}.")

    def add_mailing_list(self, mailing_list: MailingList) -> None:
        """
        Add a mailing list. Raises ValueError if its list_id is already used
        (active or archived), or if it relates to any site id that does not
        exist or is archived - see unlinkable_site_ids().
        """
        if mailing_list.list_id in self._lists:
            raise ValueError(f"Mailing list {mailing_list.list_id!r} already exists.")
        bad = self.unlinkable_site_ids(mailing_list.site_ids)
        if bad:
            raise ValueError(f"Mailing list {mailing_list.list_id!r} refers to sites that cannot be linked: "
                             + ", ".join(f"{site_id} ({why})" for site_id, why in bad.items()))
        self._lists[mailing_list.list_id] = mailing_list
        self.save_mailing_list(mailing_list)

    def add_notification(self, notification: Notification) -> None:
        """Register (and save) a newly drafted notification. Raises ValueError if its id is already known."""
        if notification.id in self._notifications:
            raise ValueError(f"Notification {notification.id} already exists.")
        self._notifications[notification.id] = notification
        self.save_notification(notification)

    def unlinkable_site_ids(self, site_ids: Iterable[int]) -> Dict[int, str]:
        """
        Cross-reference check: {site_id: reason} for every id in `site_ids`
        that a mailing list may not link to - it does not exist, or the site
        is archived. An empty dict means every id is fine.
        """
        problems = {}
        for site_id in site_ids:
            site = self._sites.get(site_id)  # .get: a missing id is an expected case here
            if site is None:
                problems[site_id] = "no such site"
            elif site.is_archived:
                problems[site_id] = "site is archived"
        return problems

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get_site(self, site_id: int) -> Site:
        """Raises KeyError (HTTP 404) if no site has this id."""
        try:
            return self._sites[site_id]
        except KeyError:
            raise KeyError(f"Site {site_id} not found.") from None

    def get_mailing_list(self, list_id: str) -> MailingList:
        """Raises KeyError (HTTP 404) if no mailing list has this id."""
        try:
            return self._lists[list_id]
        except KeyError:
            raise KeyError(f"Mailing list {list_id!r} not found.") from None

    def sites(self, include_archived: bool = False) -> Iterator[Site]:
        """Generator over sites in site_id order; archived ones only if asked for."""
        for site_id in sorted(self._sites):
            site = self._sites[site_id]
            if include_archived or not site.is_archived:
                yield site

    def mailing_lists(self, include_archived: bool = False) -> Iterator[MailingList]:
        """Generator over mailing lists in list_id order; archived ones only if asked for."""
        for list_id in sorted(self._lists):
            mailing_list = self._lists[list_id]
            if include_archived or not mailing_list.is_archived:
                yield mailing_list

    def lists_covering(self, site_id: int, include_archived: bool = False) -> Iterator[MailingList]:
        """Generator over the mailing lists related to `site_id` - active ones only unless asked otherwise."""
        return (ml for ml in self.mailing_lists(include_archived) if ml.covers(site_id))

    def get_notification(self, notification_id: str) -> Notification:
        """Raises KeyError (HTTP 404) if no notification has this id."""
        try:
            return self._notifications[notification_id]
        except KeyError:
            raise KeyError(f"Notification {notification_id} not found.") from None

    def notifications(self) -> Iterator[Notification]:
        """Generator over every notification, newest first."""
        yield from sorted(self._notifications.values(), key=lambda n: n.created_at, reverse=True)

    def next_site_id(self) -> int:
        """The id the next site created in the app gets: one above every id ever used (archived included)."""
        return max(self._sites, default=FIRST_SITE_ID - 1) + 1

    # ------------------------------------------------------------------
    # Availability checks
    # ------------------------------------------------------------------

    def check_site(self, site_id: int) -> SiteCheckOutcome:
        """
        Check one site now, record the result and save it.

        Runs inside a SiteCheckSession, so a second check of the same site
        while this one is running is refused. A site in Maintenance is
        checked (the result is stored) but keeps its status. A status change
        is followed by a notification draft when it is worth one (see
        _draft_for_change()).

        Raises:
            KeyError: unknown site_id.
            RuntimeError: no checker configured, the site is archived, or it
                is already being checked (the API layer maps this to 409).
        """
        if self._checker is None:
            raise RuntimeError("No availability checker is configured.")
        site = self.get_site(site_id)
        if site.is_archived:
            raise RuntimeError(f"{site.label} is archived - archived sites are not checked.")
        with SiteCheckSession(site) as session:
            check = self._checker.check(site.site_url)
            change = site.record_check(check)
        self.save_site(site)
        notification = self._draft_for_change(site, change) if change else None
        return SiteCheckOutcome(site.site_id, check, change, session.duration_ms, notification)

    def check_all(self) -> List[SiteCheckOutcome]:
        """
        Check every active site that is not in Maintenance, one after another,
        in site_id order. A site that is already being checked by someone else
        at that moment is skipped (that other check will record its result).
        Returns one outcome per site actually checked.
        """
        if self._checker is None:
            raise RuntimeError("No availability checker is configured.")
        outcomes = []
        for site in list(self.sites()):
            if site.status == SiteStatus.MAINTENANCE or SiteCheckSession.is_running(site.site_id):
                continue
            try:
                outcomes.append(self.check_site(site.site_id))
            except RuntimeError:
                continue  # started being checked elsewhere in the meantime
        return outcomes

    # ------------------------------------------------------------------
    # Admin create / edit
    # ------------------------------------------------------------------

    def create_site(self, actor: User, site_name: str, site_url: str, site_publish_date=None,
                    site_id: Optional[int] = None) -> Site:
        """
        Create a site from the admin page (admin only). Without an explicit
        `site_id`, the next free id is used (next_site_id()).
        `site_publish_date` may be a date, an ISO "YYYY-MM-DD" string or None.

        Raises:
            PermissionError: actor is not an admin.
            ValueError: invalid value, id already used, or URL already used
                by an active site.
        """
        require_admin(actor, "create a site")
        site = Site(site_id=site_id if site_id is not None else self.next_site_id(), site_name=site_name,
                    site_url=site_url, site_publish_date=site_publish_date)
        self.add_site(site)
        return site

    _EDITABLE_SITE_FIELDS = ("site_name", "site_url", "site_publish_date")

    def update_site(self, site_id: int, actor: User, changes: Dict[str, object]) -> Site:
        """
        Edit a site's name, URL and/or publish date (admin only). `changes`
        holds only the fields to change (a publish date of None clears it).
        All or nothing: if any value is invalid, the site is left exactly as
        it was.

        Raises:
            KeyError: unknown site_id.
            PermissionError: actor is not an admin.
            ValueError: unknown field name, invalid value, or the new URL is
                already used by another active site.
            RuntimeError: the site is archived (restore it first).
        """
        require_admin(actor, "edit a site")
        site = self.get_site(site_id)
        unknown = set(changes) - set(self._EDITABLE_SITE_FIELDS)
        if unknown:
            raise ValueError(f"These site fields cannot be edited: {', '.join(sorted(unknown))}")
        if site.is_archived:
            raise RuntimeError(f"{site.label} is archived - restore it before editing it.")
        if "site_url" in changes:
            self._ensure_url_free(Site.validate_url(changes["site_url"]), ignore_site_id=site_id)

        snapshot = {name: getattr(site, name) for name in self._EDITABLE_SITE_FIELDS}
        try:
            for name, value in changes.items():
                setattr(site, name, value)       # each property setter validates
        except ValueError:
            for name, value in snapshot.items():
                setattr(site, name, value)       # roll back the fields already changed
            raise
        self.save_site(site)
        return site

    def create_mailing_list(self, actor: User, list_id: str, name: str, members: Iterable[str] = (),
                            site_ids: Iterable[int] = ()) -> MailingList:
        """
        Create a mailing list from the admin page (admin only).

        Raises:
            PermissionError: actor is not an admin.
            ValueError: invalid value, list_id already used, or a site id that
                does not exist or is archived.
        """
        require_admin(actor, "create a mailing list")
        mailing_list = MailingList(list_id=list_id, name=name, members=members, site_ids=site_ids)
        self.add_mailing_list(mailing_list)
        return mailing_list

    def update_mailing_list(self, list_id: str, actor: User, name: Optional[str] = None,
                            members: Optional[Iterable[str]] = None,
                            site_ids: Optional[Iterable[int]] = None) -> MailingList:
        """
        Edit a mailing list (admin only). Each argument left as None is not
        changed; `members` and `site_ids` REPLACE the current sets. All values
        are validated before anything changes, so an invalid edit changes
        nothing.

        Raises:
            KeyError: unknown list_id.
            PermissionError: actor is not an admin.
            ValueError: invalid name/email/site id, or a site id that does not
                exist or is archived.
            RuntimeError: the list is archived (restore it first).
        """
        require_admin(actor, "edit a mailing list")
        mailing_list = self.get_mailing_list(list_id)
        if mailing_list.is_archived:
            raise RuntimeError(f"Mailing list {list_id!r} is archived - restore it before editing it.")

        # Validate everything first...
        new_members = None if members is None else {MailingList.normalize_email(e) for e in members}
        new_sites = None if site_ids is None else {Site.validate_site_id(i) for i in site_ids}
        if new_sites is not None:
            bad = self.unlinkable_site_ids(new_sites)
            if bad:
                raise ValueError("These sites cannot be linked: "
                                 + ", ".join(f"{site_id} ({why})" for site_id, why in sorted(bad.items())))
        if name is not None:
            mailing_list.name = name             # the setter validates; nothing else has changed yet

        # ...then apply, as set differences against the current contents.
        if new_members is not None:
            current = set(mailing_list.members)
            for email in current - new_members:
                mailing_list.remove_member(email)
            for email in new_members - current:
                mailing_list.add_member(email)
        if new_sites is not None:
            current_sites = set(mailing_list.site_ids)
            for site_id in current_sites - new_sites:
                mailing_list.unlink_site(site_id)
            for site_id in new_sites - current_sites:
                mailing_list.link_site(site_id)
        self.save_mailing_list(mailing_list)
        return mailing_list

    # ------------------------------------------------------------------
    # Incident scan (thin wrapper - the pipeline lives in app/site_scan.py)
    # ------------------------------------------------------------------

    def scan_incidents(self, incidents: Iterable) -> SiteScanResult:
        """
        Scan `incidents` (e.g. IncidentRepository.list_all()) for mentions of
        this directory's sites, by id ("Site 1042", "Site #1042", "Site-1042")
        or by full site name - see app/site_scan.py for the exact rules.
        Archived sites' names are matched too, so an incident naming an
        archived site lands in the "archived" bucket.
        """
        everything = list(self.sites(include_archived=True))
        active = [s.site_id for s in everything if not s.is_archived]
        archived = [s.site_id for s in everything if s.is_archived]
        names = {s.site_id: s.site_name for s in everything}
        return _scan_incidents(incidents, active, archived, site_names=names)

    # ------------------------------------------------------------------
    # Manual status changes and the notification process
    # ------------------------------------------------------------------

    def change_site_status(self, site_id: int, new_status: SiteStatus, reason: str,
                           actor: User) -> StatusChangeOutcome:
        """
        An admin sets a site's status by hand (e.g. starts or ends
        Maintenance, or confirms an outage), with a required reason. The
        change is saved and, when worth it, drafted as a notification.

        Raises:
            KeyError: unknown site_id.
            ValueError / PermissionError / RuntimeError: from
                Site.change_status() (bad status or blank reason / not an
                admin / site archived) - nothing changes in those cases.
        """
        site = self.get_site(site_id)
        change = site.change_status(new_status, reason, actor=actor)
        if change is None:
            return StatusChangeOutcome(site_id, None, None)
        self.save_site(site)
        return StatusChangeOutcome(site_id, change, self._draft_for_change(site, change, actor))

    def _draft_for_change(self, site: Site, change: SiteStatusChange,
                          actor: Optional[User] = None) -> Optional[Notification]:
        """
        Create (and save) the notification for one status change, addressed
        to the active lists covering the site, or return None when the change
        is not worth notifying. Any older unsent draft for the same site is
        superseded by the new one - it is out of date the moment the status
        moves again. `actor` is None for changes made by a check.
        """
        if not Notification.is_worth_notifying(change.old_status, change.new_status):
            return None
        notification = Notification.draft_for(site, change.old_status, self.mailing_lists(), created_by=actor)
        stale = [n for n in self._notifications.values()
                 if n.site_id == site.site_id and n.state == NotificationState.DRAFT]
        for old_draft in stale:
            old_draft.supersede(notification)
        self._notifications[notification.id] = notification
        if self._store:
            self._store.save_batch(notifications=[*stale, notification])
        return notification

    def send_notification(self, notification_id: str, actor: User,
                          message: Optional[str] = None) -> Notification:
        """
        Send a draft (admin only): optionally replace its text first, deliver
        it through the notifier, and only then mark it Sent. If delivery
        fails, the notification stays a Draft (with any edited text kept) and
        the delivery error propagates, so the admin can simply try again.

        Raises:
            KeyError: unknown notification id.
            PermissionError: actor is not an admin (checked before anything).
            RuntimeError: not a draft, or no notifier configured.
            ValueError: the replacement message is blank or too long.
            NotificationDeliveryError: the notifier could not deliver it.
        """
        notification = self.get_notification(notification_id)
        notification.ensure_sendable(actor)
        if self._notifier is None:
            raise RuntimeError("No notifier is configured.")
        if message is not None:
            notification.message = message
            self.save_notification(notification)
        self._notifier.deliver(notification)
        notification.mark_sent(actor)
        self.save_notification(notification)
        return notification

    def dismiss_notification(self, notification_id: str, actor: User,
                             reason: Optional[str] = None) -> Notification:
        """Discard a draft without sending it (admin only - enforced by Notification.dismiss()), and save that."""
        notification = self.get_notification(notification_id)
        notification.dismiss(actor, reason)
        self.save_notification(notification)
        return notification

    # ------------------------------------------------------------------
    # Archive / restore (with cascades)
    # ------------------------------------------------------------------

    def archive_site(self, site_id: int, actor: User) -> List[str]:
        """
        Soft-delete a site (admin only - enforced by Site.archive()) and
        unlink it from EVERY mailing list that covers it, archived lists
        included, so no list ever points at an archived site. The site
        remembers those lists for restore_site(). The site and all changed
        lists are saved in one transaction.

        Returns the ids of the lists it was unlinked from.

        Raises:
            KeyError: unknown site_id.
            PermissionError / RuntimeError: from Site.archive() (not an
                admin / already archived) - nothing is changed in that case.
        """
        site = self.get_site(site_id)
        covering = list(self.lists_covering(site_id, include_archived=True))
        list_ids = [ml.list_id for ml in covering]
        site.archive(actor, list_ids)   # validates first; raises before anything is unlinked
        for mailing_list in covering:
            mailing_list.unlink_site(site_id)
        self._save_batch(sites=[site], mailing_lists=covering)
        return list_ids

    def restore_site(self, site_id: int, actor: User) -> List[str]:
        """
        Bring an archived site back (admin only - enforced by Site.restore())
        and re-link it to the lists it was on when archived, as long as they
        still exist (lists are only ever archived, never removed, so normally
        all of them do; an archived list gets the link back too, ready for
        when that list is restored). Its status resets to Unknown. Saved in
        one transaction.

        Returns the ids of the lists it was re-linked to.

        Raises:
            KeyError: unknown site_id.
            PermissionError / RuntimeError: from Site.restore() (not an
                admin / not archived) - nothing is changed in that case.
        """
        site = self.get_site(site_id)
        remembered = site.restore(actor)
        relinked = [self._lists[list_id] for list_id in sorted(remembered) if list_id in self._lists]
        for mailing_list in relinked:
            mailing_list.link_site(site_id)
        self._save_batch(sites=[site], mailing_lists=relinked)
        return [ml.list_id for ml in relinked]

    def archive_mailing_list(self, list_id: str, actor: User) -> None:
        """
        Soft-delete a mailing list (admin only - enforced by
        MailingList.archive()). Members and site links are kept, so restoring
        it brings it back exactly as it was. Archived lists receive no
        notifications.
        """
        mailing_list = self.get_mailing_list(list_id)
        mailing_list.archive(actor)
        self.save_mailing_list(mailing_list)

    def restore_mailing_list(self, list_id: str, actor: User) -> None:
        """Bring an archived mailing list back, unchanged (admin only - enforced by MailingList.restore())."""
        mailing_list = self.get_mailing_list(list_id)
        mailing_list.restore(actor)
        self.save_mailing_list(mailing_list)

    def _save_batch(self, sites: Iterable[Site] = (), mailing_lists: Iterable[MailingList] = ()) -> None:
        """Save several objects in one transaction (no-op without a store)."""
        if self._store:
            self._store.save_batch(sites=sites, mailing_lists=mailing_lists)

    # ------------------------------------------------------------------
    # Seed loading (lenient - problems are reported, never raised)
    # ------------------------------------------------------------------

    def load_sites_from_jsonl(self, path) -> ImportReport:
        """
        Load sites from a JSONL file, line by line (never read()/readlines()).
        Raises only for problems with the file itself (e.g. FileNotFoundError);
        problems with individual records end up in the returned report.
        """
        with open(path, encoding="utf-8") as f:
            return self.load_sites_from_lines(f)

    def load_sites_from_lines(self, lines: Iterable[str]) -> ImportReport:
        """The actual site loading logic, over any iterable of JSONL lines (an open file, str.splitlines(), ...)."""
        report = ImportReport(kind="sites")
        seen_in_this_import: set = set()
        for line_number, record, error in _parse_jsonl(lines):
            if error is None:
                try:
                    site = Site.from_dict(record)
                except ValueError as exc:
                    error = str(exc)
            if error is not None:
                report.skipped_invalid.append(f"line {line_number}: {error}")
                continue
            if site.site_id in seen_in_this_import:
                report.skipped_duplicate_ids.append(site.site_id)
                continue
            seen_in_this_import.add(site.site_id)
            if site.site_id in self._sites:
                report.already_stored_ids.append(site.site_id)
                continue
            try:
                self.add_site(site)
            except ValueError as exc:      # e.g. its URL is already used by another active site
                report.skipped_invalid.append(f"line {line_number}: {exc}")
                continue
            report.created_ids.append(site.site_id)
        return report

    def load_mailing_lists_from_jsonl(self, path) -> ImportReport:
        """Load mailing lists from a JSONL file, line by line. Load the sites first - lists refer to them."""
        with open(path, encoding="utf-8") as f:
            return self.load_mailing_lists_from_lines(f)

    def load_mailing_lists_from_lines(self, lines: Iterable[str]) -> ImportReport:
        """
        The actual mailing-list loading logic. A link to a site that does not
        exist or is archived is dropped with a warning; the list itself still
        loads (see the module docstring).
        """
        report = ImportReport(kind="mailing lists")
        seen_in_this_import: set = set()
        for line_number, record, error in _parse_jsonl(lines):
            if error is None:
                try:
                    mailing_list = MailingList.from_dict(record)
                except ValueError as exc:
                    error = str(exc)
            if error is not None:
                report.skipped_invalid.append(f"line {line_number}: {error}")
                continue
            if mailing_list.list_id in seen_in_this_import:
                report.skipped_duplicate_ids.append(mailing_list.list_id)
                continue
            seen_in_this_import.add(mailing_list.list_id)
            if mailing_list.list_id in self._lists:
                report.already_stored_ids.append(mailing_list.list_id)
                continue
            for site_id, why in self.unlinkable_site_ids(mailing_list.site_ids).items():
                mailing_list.unlink_site(site_id)
                report.warnings.append(
                    f"line {line_number}: mailing list {mailing_list.list_id!r} - "
                    f"link to site {site_id} dropped ({why})"
                )
            self.add_mailing_list(mailing_list)
            report.created_ids.append(mailing_list.list_id)
        return report

    # ------------------------------------------------------------------
    # Dunder methods
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        active_sites = sum(1 for _ in self.sites())
        active_lists = sum(1 for _ in self.mailing_lists())
        return (f"<SiteDirectory: {active_sites} active sites ({len(self._sites)} total), "
                f"{active_lists} active mailing lists ({len(self._lists)} total)>")

    def __repr__(self) -> str:
        return (f"SiteDirectory(sites={len(self._sites)}, mailing_lists={len(self._lists)}, "
                f"notifications={len(self._notifications)})")


def _seed_path_from_env(env_var: str, default: Path) -> Optional[str]:
    """
    Resolve a seed file path: the env var if set, else `default`. Setting the
    env var to an empty string disables that seed file entirely (returns None).
    """
    value = os.environ.get(env_var)
    if value is None:
        return str(default)
    return value.strip() or None


def seed_site_directory(directory: SiteDirectory,
                        sites_path: Optional[str] = None,
                        lists_path: Optional[str] = None,
                        log: Callable[[str], None] = print) -> List[ImportReport]:
    """
    Startup import: load the sites seed file, then the mailing lists seed
    file, into `directory`, and log a one-line summary per file plus every
    problem found. Called once by AppState.create() (app/state.py).

    When a path argument is None it is taken from the environment:
    SITES_SEED_PATH / MAILING_LISTS_SEED_PATH, defaulting to
    data/sites.jsonl and data/mailing_lists.jsonl at the repository root.
    Setting either env var to an empty string skips that file.

    A missing file is logged as a warning and skipped - the app still starts.
    Returns the ImportReports of the files that were actually read (a file
    that was disabled or missing has no report).
    """
    sites_path = sites_path if sites_path is not None else _seed_path_from_env("SITES_SEED_PATH", DEFAULT_SITES_SEED)
    lists_path = lists_path if lists_path is not None else _seed_path_from_env(
        "MAILING_LISTS_SEED_PATH", DEFAULT_MAILING_LISTS_SEED)

    reports = []
    for path, loader, kind in ((sites_path, directory.load_sites_from_jsonl, "sites"),
                               (lists_path, directory.load_mailing_lists_from_jsonl, "mailing lists")):
        if not path:
            log(f"Seed import: {kind} seed file disabled - skipped.")
            continue
        try:
            report = loader(path)
        except FileNotFoundError:
            log(f"WARNING: Seed import: {kind} seed file not found at {path} - skipped.")
            continue
        log(f"Seed import ({path}): {report}")
        for problem in report.problems:
            log(f"  - {problem}")
        reports.append(report)
    return reports
