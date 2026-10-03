"""
Unit tests for app.models.notification.Notification (Sites & Mailing Lists
subsystem). Plain domain objects only - no HTTP, no database.

Run from backend/:  python -m pytest tests/test_models/test_notification.py -v
Uses the shared admin_user / regular_user fixtures from tests/conftest.py.
"""

import pytest

from app.models import SYSTEM_ACTOR, MailingList, Notification, NotificationState, Site, SiteStatus


@pytest.fixture
def site() -> Site:
    s = Site(site_id=1042, site_name="Customer portal", site_url="https://example.com")
    s.change_status(SiteStatus.DOWN, "HTTP 503")
    return s


@pytest.fixture
def lists():
    return [
        MailingList("ops-team", "Ops", members=["a@example.com", "shared@example.com"], site_ids=[1042]),
        MailingList("management", "Mgmt", members=["shared@example.com", "b@example.com"], site_ids=[1042, 7]),
        MailingList("other-site", "Other", members=["c@example.com"], site_ids=[7]),
    ]


def test_draft_for_targets_only_lists_covering_the_site(site, lists, admin_user):
    n = Notification.draft_for(site, SiteStatus.UNKNOWN, lists, created_by=admin_user)
    assert n.state == NotificationState.DRAFT
    assert n.list_ids == ("management", "ops-team")
    # union of members: the shared address appears once
    assert n.recipients == {"a@example.com", "b@example.com", "shared@example.com"}
    assert (n.site_id, n.site_name, n.old_status, n.new_status) == (1042, "Customer portal",
                                                                    SiteStatus.UNKNOWN, SiteStatus.DOWN)
    assert n.created_by == admin_user.username
    assert n.message == "Site 1042 (Customer portal) changed from Unknown to Down."


def test_draft_from_an_automated_check_is_attributed_to_system(site, lists):
    assert Notification.draft_for(site, SiteStatus.UNKNOWN, lists).created_by == SYSTEM_ACTOR


def test_archived_lists_are_ignored(site, lists, admin_user):
    lists[0].archive(admin_user)
    n = Notification.draft_for(site, SiteStatus.UNKNOWN, lists)
    assert n.list_ids == ("management",)


def test_no_covering_list_means_skipped(site):
    n = Notification.draft_for(site, SiteStatus.UNKNOWN, [MailingList("x", "X", ["x@example.com"], [7])])
    assert n.state == NotificationState.SKIPPED
    assert n.is_final


def test_covering_list_with_no_members_means_skipped(site):
    n = Notification.draft_for(site, SiteStatus.UNKNOWN, [MailingList("empty", "Empty", [], [1042])])
    assert n.state == NotificationState.SKIPPED


def test_notification_is_a_snapshot(site, lists, admin_user):
    n = Notification.draft_for(site, SiteStatus.UNKNOWN, lists)
    lists[0].add_member("late@example.com")
    site.site_name = "Renamed"
    assert "late@example.com" not in n.recipients
    assert n.site_name == "Customer portal"


def test_mark_sent(site, lists, admin_user):
    n = Notification.draft_for(site, SiteStatus.UNKNOWN, lists)
    n.mark_sent(admin_user)
    assert n.state == NotificationState.SENT
    assert n.decided_by == admin_user.username and n.decided_at is not None


def test_dismiss_with_and_without_reason(site, lists, admin_user):
    n1 = Notification.draft_for(site, SiteStatus.UNKNOWN, lists)
    n1.dismiss(admin_user, "  Recovered within a minute  ")
    assert n1.state == NotificationState.DISMISSED
    assert n1.dismiss_reason == "Recovered within a minute"
    n2 = Notification.draft_for(site, SiteStatus.UNKNOWN, lists)
    n2.dismiss(admin_user, "   ")
    assert n2.dismiss_reason is None


@pytest.mark.parametrize("decide", ["send", "dismiss"])
def test_final_notifications_cannot_change(site, lists, admin_user, decide):
    n = Notification.draft_for(site, SiteStatus.UNKNOWN, lists)
    n.mark_sent(admin_user) if decide == "send" else n.dismiss(admin_user)
    with pytest.raises(RuntimeError, match="already"):
        n.mark_sent(admin_user)
    with pytest.raises(RuntimeError, match="already"):
        n.dismiss(admin_user)
    with pytest.raises(RuntimeError, match="draft"):
        n.message = "edited after the fact"


def test_skipped_cannot_be_sent(site, admin_user):
    n = Notification.draft_for(site, SiteStatus.UNKNOWN, [])
    with pytest.raises(RuntimeError, match="already skipped"):
        n.mark_sent(admin_user)


def test_only_admin_can_send_or_dismiss(site, lists, regular_user):
    n = Notification.draft_for(site, SiteStatus.UNKNOWN, lists)
    with pytest.raises(PermissionError):
        n.mark_sent(regular_user)
    with pytest.raises(PermissionError):
        n.dismiss(regular_user)
    assert n.state == NotificationState.DRAFT


def test_draft_message_is_editable_and_validated(site, lists):
    n = Notification.draft_for(site, SiteStatus.UNKNOWN, lists)
    n.message = "  Portal is down, we are on it.  "
    assert n.message == "Portal is down, we are on it."
    with pytest.raises(ValueError, match="required"):
        n.message = "   "
    with pytest.raises(ValueError, match="at most"):
        n.message = "x" * 2001


def test_constructor_rejects_non_status_values():
    with pytest.raises(ValueError, match="SiteStatus"):
        Notification(1, "x", "down", SiteStatus.DOWN, [], [], "msg")


def test_str_and_repr(site, lists):
    n = Notification.draft_for(site, SiteStatus.UNKNOWN, lists)
    assert str(n) == "[Notification - DRAFT] Site 1042: UNKNOWN -> DOWN, 3 recipients"
    assert repr(n).startswith(f"Notification(id={n.id[:8]}, site_id=1042, state=DRAFT")
