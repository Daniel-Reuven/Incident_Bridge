"""
Tests for the notification process in SiteDirectory (app/sites.py):
drafts from checks and manual changes, superseding, sending through a
Notifier (and what happens when delivery fails), dismissing, and saving.

Run from backend/:  python -m pytest tests/test_sites_notifications.py -v
Uses FakeChecker and OutboxNotifier - no network, no email.
Uses the shared admin_user / regular_user fixtures from tests/conftest.py.
"""

import pytest

from app.models import MailingList, NotificationState, Site, SiteStatus
from app.persistence_sites import SqliteSiteStore
from app.services import FakeChecker, NotificationDeliveryError, Notifier, OutboxNotifier
from app.sites import SiteDirectory

PORTAL = "https://portal.example.com"


class FailingNotifier(Notifier):
    def deliver(self, notification):
        raise NotificationDeliveryError("mail server unreachable")


@pytest.fixture
def store() -> SqliteSiteStore:
    return SqliteSiteStore(":memory:")


def make_directory(store, script=None, notifier=None) -> SiteDirectory:
    d = SiteDirectory(store, checker=FakeChecker(script or {}),
                      notifier=notifier if notifier is not None else OutboxNotifier(log=None))
    d.add_site(Site(1001, "Portal", PORTAL))
    d.add_site(Site(1002, "Store", "https://store.example.com"))
    d.add_mailing_list(MailingList("ops", "Ops", ["ops@example.com", "noc@example.com"], [1001, 1002]))
    d.add_mailing_list(MailingList("mgmt", "Mgmt", ["cto@example.com"], [1001]))
    return d


def saved_notifications(store):
    return {n.id: n for n in store.load_all().notifications}


# --- drafting ------------------------------------------------------------

def test_first_successful_check_drafts_nothing(store):
    d = make_directory(store)
    outcome = d.check_site(1001)
    assert outcome.change is not None and outcome.notification is None
    assert list(d.notifications()) == []


def test_a_site_going_down_drafts_to_every_covering_list(store):
    d = make_directory(store, {PORTAL: [200, None, None]})
    d.check_site(1001)
    degraded = d.check_site(1001).notification
    down = d.check_site(1001).notification
    assert down.state == NotificationState.DRAFT
    assert down.list_ids == ("mgmt", "ops")
    assert down.recipients == {"ops@example.com", "noc@example.com", "cto@example.com"}
    assert (down.old_status, down.new_status, down.created_by) == (SiteStatus.DEGRADED, SiteStatus.DOWN, "system")
    assert degraded.state == NotificationState.DISMISSED   # superseded by the Down draft


def test_a_newer_draft_supersedes_older_drafts_of_the_same_site_only(store, admin_user):
    d = make_directory(store)
    portal_down = d.change_site_status(1001, SiteStatus.DOWN, "outage", admin_user).notification
    store_down = d.change_site_status(1002, SiteStatus.DOWN, "outage", admin_user).notification
    portal_up = d.change_site_status(1001, SiteStatus.OPERATIONAL, "fixed", admin_user).notification
    assert portal_down.state == NotificationState.DISMISSED
    assert portal_down.dismiss_reason == "Superseded by a newer status change (now Operational)."
    assert store_down.state == NotificationState.DRAFT
    assert portal_up.state == NotificationState.DRAFT
    assert saved_notifications(store)[portal_down.id].state == NotificationState.DISMISSED


def test_sent_notifications_are_never_superseded(store, admin_user):
    d = make_directory(store)
    down = d.change_site_status(1001, SiteStatus.DOWN, "outage", admin_user).notification
    d.send_notification(down.id, admin_user)
    d.change_site_status(1001, SiteStatus.OPERATIONAL, "fixed", admin_user)
    assert down.state == NotificationState.SENT


def test_no_active_covering_list_means_skipped(store, admin_user):
    d = make_directory(store)
    d.archive_mailing_list("ops", admin_user)
    n = d.change_site_status(1002, SiteStatus.DOWN, "outage", admin_user).notification
    assert n.state == NotificationState.SKIPPED and n.list_ids == ()


def test_manual_change_drafts_with_the_admin_as_creator(store, admin_user):
    d = make_directory(store)
    outcome = d.change_site_status(1001, SiteStatus.MAINTENANCE, "Planned upgrade", admin_user)
    assert outcome.change.new_status == SiteStatus.MAINTENANCE
    assert outcome.notification.created_by == admin_user.username
    assert store.load_all().sites[0].status == SiteStatus.MAINTENANCE


def test_manual_change_to_the_same_status_does_nothing(store, admin_user):
    d = make_directory(store)
    d.change_site_status(1001, SiteStatus.DOWN, "outage", admin_user)
    again = d.change_site_status(1001, SiteStatus.DOWN, "still out", admin_user)
    assert again.change is None and again.notification is None
    assert len(list(d.notifications())) == 1


def test_manual_change_errors_change_nothing(store, regular_user, admin_user):
    d = make_directory(store)
    with pytest.raises(PermissionError):
        d.change_site_status(1001, SiteStatus.DOWN, "x", regular_user)
    with pytest.raises(ValueError, match="reason"):
        d.change_site_status(1001, SiteStatus.DOWN, " ", admin_user)
    with pytest.raises(KeyError):
        d.change_site_status(9999, SiteStatus.DOWN, "x", admin_user)
    assert list(d.notifications()) == []


# --- sending ---------------------------------------------------------------

def test_send_delivers_then_marks_sent_and_saves(store, admin_user):
    notifier = OutboxNotifier(log=None)
    d = make_directory(store, notifier=notifier)
    n = d.change_site_status(1001, SiteStatus.DOWN, "outage", admin_user).notification
    d.send_notification(n.id, admin_user, message="  Portal is down - we are on it.  ")
    assert n.state == NotificationState.SENT and n.decided_by == admin_user.username
    assert notifier.outbox[0].body == "Portal is down - we are on it."
    saved = saved_notifications(store)[n.id]
    assert saved.state == NotificationState.SENT and saved.message == "Portal is down - we are on it."


def test_failed_delivery_leaves_a_draft_that_can_be_retried(store, admin_user):
    d = make_directory(store, notifier=FailingNotifier())
    n = d.change_site_status(1001, SiteStatus.DOWN, "outage", admin_user).notification
    with pytest.raises(NotificationDeliveryError, match="unreachable"):
        d.send_notification(n.id, admin_user, message="Edited text")
    assert n.state == NotificationState.DRAFT
    assert saved_notifications(store)[n.id].message == "Edited text"   # the edit was kept
    d._notifier = OutboxNotifier(log=None)                               # e.g. the mail server is back
    d.send_notification(n.id, admin_user)
    assert n.state == NotificationState.SENT


def test_permission_is_checked_before_anything_is_delivered(store, admin_user, regular_user):
    notifier = OutboxNotifier(log=None)
    d = make_directory(store, notifier=notifier)
    n = d.change_site_status(1001, SiteStatus.DOWN, "outage", admin_user).notification
    with pytest.raises(PermissionError):
        d.send_notification(n.id, regular_user, message="sneaky edit")
    assert notifier.outbox == [] and n.message != "sneaky edit"


def test_send_errors(store, admin_user):
    d = make_directory(store)
    n = d.change_site_status(1001, SiteStatus.DOWN, "outage", admin_user).notification
    with pytest.raises(ValueError, match="required"):
        d.send_notification(n.id, admin_user, message="   ")
    d.send_notification(n.id, admin_user)
    with pytest.raises(RuntimeError, match="already sent"):
        d.send_notification(n.id, admin_user)
    with pytest.raises(KeyError):
        d.send_notification("nope", admin_user)


def test_send_without_a_notifier(store, admin_user):
    d = make_directory(store)
    d._notifier = None
    n = d.change_site_status(1001, SiteStatus.DOWN, "outage", admin_user).notification
    with pytest.raises(RuntimeError, match="No notifier"):
        d.send_notification(n.id, admin_user)


# --- dismissing ---------------------------------------------------------

def test_dismiss_is_saved(store, admin_user, regular_user):
    d = make_directory(store)
    n = d.change_site_status(1001, SiteStatus.DOWN, "outage", admin_user).notification
    with pytest.raises(PermissionError):
        d.dismiss_notification(n.id, regular_user)
    d.dismiss_notification(n.id, admin_user, "Recovered within a minute")
    saved = saved_notifications(store)[n.id]
    assert saved.state == NotificationState.DISMISSED and saved.dismiss_reason == "Recovered within a minute"
