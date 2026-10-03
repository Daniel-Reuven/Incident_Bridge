"""
Notification: a message to the mailing lists that cover a site, created
when that site's status changes. Part of the Sites & Mailing Lists
subsystem (group-of-four extension).

Lifecycle (NotificationState in enums.py):

    created ──> DRAFT ──> SENT        (an admin sends it)
           │         ├──> DISMISSED   (an admin discards it)
           │         └──> DISMISSED   (superseded: a newer status change of the
           │                           same site replaced it - see supersede())
           └──> SKIPPED               (no active list with members covers the site)

Notifications are drafted, never sent automatically, so a site that flaps
up and down cannot spam the lists. Not every status change is drafted:
see is_worth_notifying() (e.g. a never-checked site turning out to be fine
is not news).

A Notification is a SNAPSHOT: it copies the site's id/name, the lists'
ids and the combined recipient addresses at the moment it is drafted. It
keeps no live references to Site or MailingList objects, so archiving a
site or editing a list later never changes what an old notification says
or who it went to.

This class only records state. Actually delivering a notification is the
Notifier service's job (app/services/notifier.py). SiteDirectory's
send_notification() (app/sites.py) runs ensure_sendable(), has the notifier
deliver, and only then calls mark_sent() - so a failed delivery leaves the
notification a Draft that can be sent again.
"""

import uuid
from datetime import datetime, timezone
from typing import FrozenSet, Iterable, Optional, Tuple

from app.models._permissions import require_admin
from app.models.enums import NotificationState, SiteStatus
from app.models.mailing_list import MailingList
from app.models.site import SYSTEM_ACTOR, Site
from app.models.user import User

_MAX_MESSAGE_LENGTH = 2000


class Notification:
    """
    A drafted, sent, dismissed or skipped notification about one site status
    change. See the module docstring for the lifecycle and snapshot rules.

    Normally created with Notification.draft_for(site, old_status, lists,
    created_by). The plain constructor takes the snapshot values directly;
    it exists for that classmethod and for from_persisted(), which rebuilds
    stored notifications (app/persistence_sites.py).
    """

    def __init__(self, site_id: int, site_name: str, old_status: SiteStatus, new_status: SiteStatus,
                 list_ids: Iterable[str], recipients: Iterable[str], message: str,
                 created_by: str = SYSTEM_ACTOR):
        if not isinstance(old_status, SiteStatus) or not isinstance(new_status, SiteStatus):
            raise ValueError("old_status and new_status must be SiteStatus values.")
        self.id = str(uuid.uuid4())
        self.site_id = site_id
        self.site_name = site_name
        self.old_status = old_status
        self.new_status = new_status
        self.list_ids: Tuple[str, ...] = tuple(sorted(set(list_ids)))
        self.recipients: FrozenSet[str] = frozenset(recipients)
        self.message = message  # validated by the property setter
        self.created_by = created_by
        self.created_at = datetime.now(timezone.utc)
        self.decided_by: Optional[str] = None
        self.decided_at: Optional[datetime] = None
        self.dismiss_reason: Optional[str] = None
        # Nobody to notify -> nothing for an admin to decide.
        self._state = NotificationState.DRAFT if self.recipients else NotificationState.SKIPPED

    # ------------------------------------------------------------------
    # Alternate constructor
    # ------------------------------------------------------------------

    @classmethod
    def draft_for(cls, site: Site, old_status: SiteStatus, lists: Iterable[MailingList],
                  created_by: Optional[User] = None) -> "Notification":
        """
        Build the notification for `site`'s change from `old_status` to its
        current status.

        Only lists that are active (not archived) AND cover the site are
        used; the recipients are the union of their members (an address on
        two lists receives one copy). The caller may pass every list it has -
        the filtering happens here. If nothing remains, the notification
        starts as SKIPPED. `created_by` is None for a change made by an
        automated check.
        """
        relevant = [ml for ml in lists if not ml.is_archived and ml.covers(site.site_id)]
        recipients = frozenset().union(*(ml.recipients for ml in relevant))
        return cls(
            site_id=site.site_id,
            site_name=site.site_name,
            old_status=old_status,
            new_status=site.status,
            list_ids=(ml.list_id for ml in relevant),
            recipients=recipients,
            message=cls.default_message(site, old_status, site.status),
            created_by=created_by.username if created_by else SYSTEM_ACTOR,
        )

    @classmethod
    def from_persisted(cls, notification_id: str, site_id: int, site_name: str, old_status: SiteStatus,
                       new_status: SiteStatus, list_ids: Iterable[str], recipients: Iterable[str],
                       message: str, state: NotificationState, created_by: str, created_at: datetime,
                       decided_by: Optional[str] = None, decided_at: Optional[datetime] = None,
                       dismiss_reason: Optional[str] = None) -> "Notification":
        """
        Rebuild a Notification exactly as it was stored - used ONLY by the
        site store (app/persistence_sites.py) at startup. The constructor
        would mint a new id and derive the state from the recipients, so
        both are overwritten with the stored values afterwards.
        """
        if not isinstance(state, NotificationState):
            raise ValueError(f"state must be a NotificationState, got {state!r}")
        notification = cls(site_id=site_id, site_name=site_name, old_status=old_status, new_status=new_status,
                           list_ids=list_ids, recipients=recipients, message=message, created_by=created_by)
        notification.id = notification_id
        notification._state = state
        notification.created_at = created_at
        notification.decided_by = decided_by
        notification.decided_at = decided_at
        notification.dismiss_reason = dismiss_reason
        return notification

    @staticmethod
    def is_worth_notifying(old_status: SiteStatus, new_status: SiteStatus) -> bool:
        """
        Whether a status change deserves a notification at all. Everything is,
        except changes that carry no news for the people on the lists:
          - Unknown -> Operational: a new or restored site was checked and is
            fine (otherwise the first "Check all" would draft one per site);
          - anything -> Unknown: only happens when a site is restored from the
            archive, before it has been checked.
        """
        if new_status == SiteStatus.UNKNOWN:
            return False
        return not (old_status == SiteStatus.UNKNOWN and new_status == SiteStatus.OPERATIONAL)

    @staticmethod
    def default_message(site: Site, old_status: SiteStatus, new_status: SiteStatus) -> str:
        """The pre-filled message text, e.g. 'Site 1042 (Customer portal) changed from Operational to Down.'"""
        def label(status: SiteStatus) -> str:
            return status.value.replace("_", " ").capitalize()
        return f"{site.label} ({site.site_name}) changed from {label(old_status)} to {label(new_status)}."

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> NotificationState:
        """Read-only: changes only through mark_sent() / dismiss()."""
        return self._state

    @property
    def message(self) -> str:
        return self._message

    @message.setter
    def message(self, value) -> None:
        """
        The text sent to the lists. Editable only while the notification is
        a DRAFT (an admin may adjust the pre-filled text before sending).
        Raises ValueError if blank or too long, RuntimeError once decided.
        """
        if not isinstance(value, str) or not value.strip():
            raise ValueError("A notification message is required.")
        value = value.strip()
        if len(value) > _MAX_MESSAGE_LENGTH:
            raise ValueError(f"A notification message must be at most {_MAX_MESSAGE_LENGTH} characters.")
        # hasattr: during __init__ the state is not set yet, and the first
        # assignment must always be allowed.
        if hasattr(self, "_state") and self._state != NotificationState.DRAFT:
            raise RuntimeError("Only a draft notification's message can be edited.")
        self._message = value

    @property
    def is_final(self) -> bool:
        """True once nothing can change it any more (sent, dismissed or skipped)."""
        return self._state != NotificationState.DRAFT

    # ------------------------------------------------------------------
    # Business operations
    # ------------------------------------------------------------------

    def ensure_sendable(self, actor: User) -> None:
        """
        Check, WITHOUT changing anything, that `actor` may send this
        notification right now: an admin, and the notification is still a
        DRAFT. Called before delivery, so nothing is ever delivered that
        mark_sent() would then refuse to record.

        Raises:
            PermissionError: if actor is not an admin.
            RuntimeError: if the notification is not a DRAFT.
        """
        require_admin(actor, "send a notification")
        self._require_draft()

    def mark_sent(self, actor: User) -> None:
        """
        Record that the draft was delivered (admin only). Called by
        SiteDirectory.send_notification() AFTER the notifier has delivered
        it - not directly by the API.

        Raises:
            PermissionError: if actor is not an admin.
            RuntimeError: if the notification is not a DRAFT.
        """
        self.ensure_sendable(actor)
        self._decide(NotificationState.SENT, actor)

    def dismiss(self, actor: User, reason: Optional[str] = None) -> None:
        """
        Discard a draft without sending it (admin only). `reason` is optional
        - dismissing is a routine "not worth sending" decision.

        Raises:
            PermissionError: if actor is not an admin.
            RuntimeError: if the notification is not a DRAFT.
        """
        require_admin(actor, "dismiss a notification")
        self._require_draft()
        self.dismiss_reason = reason.strip() if reason and reason.strip() else None
        self._decide(NotificationState.DISMISSED, actor)

    def supersede(self, newer: "Notification") -> None:
        """
        Automatically retire this DRAFT because `newer` (a later status change
        of the same site) replaces it - e.g. a "Down" draft nobody sent before
        the site recovered. It becomes DISMISSED, decided by "system", with
        the reason saying what replaced it. A system action, so no actor and
        no permission check.

        Raises:
            ValueError: if `newer` is about a different site.
            RuntimeError: if this notification is not a DRAFT.
        """
        if newer.site_id != self.site_id:
            raise ValueError("A notification can only be superseded by one about the same site.")
        self._require_draft()
        label = newer.new_status.value.replace("_", " ").capitalize()
        self.dismiss_reason = f"Superseded by a newer status change (now {label})."
        self._state = NotificationState.DISMISSED
        self.decided_by = SYSTEM_ACTOR
        self.decided_at = datetime.now(timezone.utc)

    def _require_draft(self) -> None:
        if self._state != NotificationState.DRAFT:
            raise RuntimeError(f"This notification is already {self._state.value}.")

    def _decide(self, new_state: NotificationState, actor: User) -> None:
        self._state = new_state
        self.decided_by = actor.username
        self.decided_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Dunder methods
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        return (f"[Notification - {self._state.name}] Site {self.site_id}: "
                f"{self.old_status.name} -> {self.new_status.name}, {len(self.recipients)} recipients")

    def __repr__(self) -> str:
        return (f"Notification(id={self.id[:8]}, site_id={self.site_id}, state={self._state.name}, "
                f"list_ids={self.list_ids!r}, recipients={len(self.recipients)})")
