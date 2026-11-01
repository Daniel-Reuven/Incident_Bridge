"""
Tests for archiving site-portal notifications: a sent, dismissed or skipped
notification moves to the Archive tab (view only) NOTIFICATION_ARCHIVE_DAYS
after it was finished; a draft never does.

Covers the model (Notification.finished_at / archive_at / is_archived), the
directory and its setting (SiteDirectory.notifications(archived=...),
notification_archive_after_from_env()) and the API (?archived=true).

Run from backend/:  python -m pytest tests/test_notification_archive.py -v
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import MailingList, Notification, NotificationState, Site, SiteStatus
from app.services import OutboxNotifier
from app.sites import SiteDirectory, notification_archive_after_from_env

DAY = timedelta(days=1)
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def make_notification(lists=True) -> Notification:
    """A draft (or, with lists=False, a skipped notification) about site 1001 going down."""
    site = Site(1001, "Portal", "https://portal.example.com")
    site.change_status(SiteStatus.DOWN, "HTTP 503")
    covering = [MailingList("ops", "Ops", ["ops@example.com"], [1001])] if lists else []
    return Notification.draft_for(site, SiteStatus.OPERATIONAL, covering)


def finished_days_ago(notification: Notification, days: float) -> Notification:
    """Move the moment it was finished into the past (decided_at, or created_at for a skipped one)."""
    when = NOW - timedelta(days=days)
    if notification.decided_at is None:
        notification.created_at = when
    else:
        notification.decided_at = when
    return notification


# --- the model ------------------------------------------------------------------

def test_a_draft_is_never_archived_however_old(admin_user):
    draft = make_notification()
    draft.created_at = NOW - 365 * DAY
    assert draft.finished_at is None and draft.archive_at(30 * DAY) is None
    assert not draft.is_archived(30 * DAY, now=NOW)


@pytest.mark.parametrize("finish", ["sent", "dismissed", "skipped"])
def test_finished_notifications_archive_after_the_period(admin_user, finish):
    notification = make_notification(lists=finish != "skipped")
    if finish == "sent":
        notification.mark_sent(admin_user)
    elif finish == "dismissed":
        notification.dismiss(admin_user, "false alarm")
    assert notification.state.value == finish

    finished_days_ago(notification, 29)
    assert notification.archive_at(30 * DAY) == notification.finished_at + 30 * DAY
    assert not notification.is_archived(30 * DAY, now=NOW)
    finished_days_ago(notification, 30)
    assert notification.is_archived(30 * DAY, now=NOW)            # exactly at the moment it moves


def test_a_skipped_notification_counts_from_when_it_was_created():
    skipped = make_notification(lists=False)
    assert skipped.state == NotificationState.SKIPPED and skipped.decided_at is None
    assert skipped.finished_at == skipped.created_at


def test_archiving_off_never_archives(admin_user):
    sent = make_notification()
    sent.mark_sent(admin_user)
    finished_days_ago(sent, 1000)
    assert sent.archive_at(None) is None and not sent.is_archived(None, now=NOW)


# --- the directory and the setting -------------------------------------------------

def test_the_directory_splits_current_and_archived(admin_user):
    directory = SiteDirectory(notifier=OutboxNotifier(log=None), archive_notifications_after=30 * DAY)
    old_sent, new_sent, old_draft = make_notification(), make_notification(), make_notification()
    for n in (old_sent, new_sent, old_draft):
        directory._notifications[n.id] = n
    old_sent.mark_sent(admin_user)
    new_sent.mark_sent(admin_user)
    old_sent.decided_at = datetime.now(timezone.utc) - 31 * DAY
    old_draft.created_at = datetime.now(timezone.utc) - 400 * DAY     # waiting for a decision: stays current

    assert {n.id for n in directory.notifications(archived=True)} == {old_sent.id}
    assert {n.id for n in directory.notifications(archived=False)} == {new_sent.id, old_draft.id}
    assert len(list(directory.notifications())) == 3                  # the default still lists everything


@pytest.mark.parametrize("value, expected", [
    (None, 30 * DAY), ("", 30 * DAY), ("7", 7 * DAY), ("0.5", timedelta(hours=12)), ("0", None),
])
def test_the_setting(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("NOTIFICATION_ARCHIVE_DAYS", raising=False)
    else:
        monkeypatch.setenv("NOTIFICATION_ARCHIVE_DAYS", value)
    assert notification_archive_after_from_env() == expected


@pytest.mark.parametrize("value", ["-1", "soon", "nan"])
def test_a_bad_setting_stops_startup(monkeypatch, value):
    monkeypatch.setenv("NOTIFICATION_ARCHIVE_DAYS", value)
    with pytest.raises(ValueError, match="NOTIFICATION_ARCHIVE_DAYS must be a number of days"):
        notification_archive_after_from_env()


# --- the API ---------------------------------------------------------------------

@pytest.fixture
def portal(admin_client):
    """admin_client with a memory-only site directory that archives after 30 days."""
    from app.api.app import app
    directory = SiteDirectory(notifier=OutboxNotifier(log=None), archive_notifications_after=30 * DAY)
    directory.add_site(Site(1001, "Portal", "https://portal.example.com"))
    directory.add_site(Site(1002, "Store", "https://store.example.com"))
    directory.add_mailing_list(MailingList("ops", "Ops", ["ops@example.com"], [1001, 1002]))
    app.state.incident_bridge.sites = directory
    return admin_client


def test_archived_notifications_move_to_their_own_list_and_stay_view_only(portal):
    from app.api.app import app
    old = portal.post("/sites/1001/status", json={"status": "down", "reason": "x"}).json()["notification"]
    pending = portal.post("/sites/1002/status", json={"status": "down", "reason": "x"}).json()["notification"]
    sent = portal.post(f"/sites/notifications/{old['id']}/send", json={}).json()
    assert sent["archived"] is False and sent["archive_at"] is not None
    assert pending["archived"] is False and pending["archive_at"] is None        # a draft never archives

    # 31 days later (moved back in time, rather than waiting)
    app.state.incident_bridge.sites.get_notification(old["id"]).decided_at -= 31 * DAY

    current = portal.get("/sites/notifications").json()
    archived = portal.get("/sites/notifications?archived=true").json()
    assert [n["id"] for n in current] == [pending["id"]]
    assert [(n["id"], n["state"], n["archived"]) for n in archived] == [(old["id"], "sent", True)]
    assert portal.get("/sites/notifications?archived=true&state=draft").json() == []

    # view only: it can no longer be sent or dismissed (it is not a draft)
    assert portal.post(f"/sites/notifications/{old['id']}/dismiss", json={}).status_code == 409
    # the site's own history still shows it, flagged as archived
    history = portal.get("/sites/1001").json()["notifications"]
    assert [(n["id"], n["archived"]) for n in history] == [(old["id"], True)]
