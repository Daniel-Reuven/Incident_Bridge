"""
Unit tests for the notification-process additions to
app/models/notification.py: is_worth_notifying(), ensure_sendable() and
supersede().

Run from backend/:  python -m pytest tests/test_models/test_notification_process.py -v
Uses the shared admin_user / regular_user fixtures from tests/conftest.py.
"""

import pytest

from app.models import SYSTEM_ACTOR, MailingList, Notification, NotificationState, Site, SiteStatus

S = SiteStatus


@pytest.mark.parametrize("old, new, expected", [
    (S.UNKNOWN, S.OPERATIONAL, False),     # first check of a new/restored site: fine, no news
    (S.DOWN, S.UNKNOWN, False),            # restore from archive
    (S.UNKNOWN, S.DOWN, True),
    (S.UNKNOWN, S.DEGRADED, True),
    (S.OPERATIONAL, S.DEGRADED, True),
    (S.DEGRADED, S.DOWN, True),
    (S.DOWN, S.OPERATIONAL, True),         # recovery is news
    (S.OPERATIONAL, S.MAINTENANCE, True),
    (S.MAINTENANCE, S.OPERATIONAL, True),
])
def test_is_worth_notifying(old, new, expected):
    assert Notification.is_worth_notifying(old, new) is expected


def make(site_id=1042, new=S.DOWN) -> Notification:
    site = Site(site_id, "Portal", "https://example.com")
    site.change_status(new, "x")
    return Notification.draft_for(site, S.OPERATIONAL, [MailingList("ops", "Ops", ["a@example.com"], [site_id])])


def test_ensure_sendable_changes_nothing(admin_user, regular_user):
    n = make()
    n.ensure_sendable(admin_user)
    assert n.state == NotificationState.DRAFT
    with pytest.raises(PermissionError):
        n.ensure_sendable(regular_user)
    n.dismiss(admin_user)
    with pytest.raises(RuntimeError, match="already dismissed"):
        n.ensure_sendable(admin_user)


def test_supersede_dismisses_the_old_draft_as_system():
    old, newer = make(new=S.DOWN), make(new=S.OPERATIONAL)
    old.supersede(newer)
    assert old.state == NotificationState.DISMISSED
    assert old.decided_by == SYSTEM_ACTOR and old.decided_at is not None
    assert old.dismiss_reason == "Superseded by a newer status change (now Operational)."


def test_supersede_rules(admin_user):
    old = make()
    with pytest.raises(ValueError, match="same site"):
        old.supersede(make(site_id=7))
    old.mark_sent(admin_user)
    with pytest.raises(RuntimeError, match="already sent"):
        old.supersede(make())
