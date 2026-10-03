"""
Unit tests for the from_persisted() alternate constructors on Site,
MailingList and Notification - the ones the site store
(app/persistence_sites.py) uses to rebuild saved objects at startup.

Run from backend/:  python -m pytest tests/test_models/test_from_persisted.py -v
"""

from datetime import date, datetime, timezone

import pytest

from app.models import MailingList, Notification, NotificationState, Site, SiteStatus, SiteStatusChange

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)


def test_site_state_is_restored_without_new_history():
    history = [SiteStatusChange(SiteStatus.UNKNOWN, SiteStatus.DOWN, "HTTP 503", "system", T0)]
    site = Site.from_persisted(1042, "Portal", "https://example.com", date(2025, 3, 1), SiteStatus.DOWN,
                               history, created_at=T0, archived_at=T1, archived_list_ids=["ops"])
    assert site.status == SiteStatus.DOWN
    assert site.status_history == tuple(history)
    assert (site.created_at, site.archived_at) == (T0, T1)
    assert site.is_archived and site.archived_list_ids == frozenset({"ops"})


def test_site_identity_fields_are_still_validated():
    with pytest.raises(ValueError, match="site_url"):
        Site.from_persisted(1, "Portal", "not-a-url", None, SiteStatus.UNKNOWN, [], created_at=T0)


def test_mailing_list_is_restored():
    ml = MailingList.from_persisted("ops", "Ops", ["a@example.com"], [1042], created_at=T0, archived_at=T1)
    assert (ml.members, ml.site_ids, ml.created_at, ml.archived_at) == (("a@example.com",), (1042,), T0, T1)
    assert ml.is_archived


def test_notification_keeps_its_id_and_state():
    n = Notification.from_persisted("abc-123", 1042, "Portal", SiteStatus.UNKNOWN, SiteStatus.DOWN, ["ops"],
                                    ["a@example.com"], "msg", NotificationState.SENT, "admin", T0,
                                    decided_by="admin", decided_at=T1)
    assert (n.id, n.state, n.created_at, n.decided_by, n.decided_at) == ("abc-123", NotificationState.SENT,
                                                                         T0, "admin", T1)
    assert n.is_final


def test_notification_rejects_a_bad_state():
    with pytest.raises(ValueError, match="NotificationState"):
        Notification.from_persisted("x", 1, "P", SiteStatus.UNKNOWN, SiteStatus.DOWN, [], [], "m",
                                    "sent", "admin", T0)
