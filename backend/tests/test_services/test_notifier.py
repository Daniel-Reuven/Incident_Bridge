"""
Unit tests for app/services/notifier.py: the Notifier interface and the
in-app OutboxNotifier.

Run from backend/:  python -m pytest tests/test_services/test_notifier.py -v
"""

import pytest

from app.models import MailingList, Notification, Site, SiteStatus
from app.services import NotificationDeliveryError, Notifier, OutboxNotifier


@pytest.fixture
def notification() -> Notification:
    site = Site(1042, "Customer portal", "https://example.com")
    site.change_status(SiteStatus.DOWN, "HTTP 503")
    return Notification.draft_for(site, SiteStatus.OPERATIONAL,
                                  [MailingList("ops", "Ops", ["b@example.com", "a@example.com"], [1042])])


def test_the_interface_cannot_be_instantiated():
    with pytest.raises(TypeError):
        Notifier()


def test_subject_line(notification):
    assert Notifier.subject_for(notification) == "[Incident Bridge] Site 1042 (Customer portal) is now Down"


def test_outbox_records_the_delivery_and_logs_it(notification):
    lines = []
    notifier = OutboxNotifier(log=lines.append)
    notifier.deliver(notification)
    (message,) = notifier.outbox
    assert message.notification_id == notification.id
    assert message.recipients == ("a@example.com", "b@example.com")
    assert message.body == notification.message
    assert message.subject.endswith("is now Down")
    assert lines == ["Notification delivered (outbox): [Incident Bridge] Site 1042 (Customer portal) "
                     "is now Down -> 2 recipients"]
    assert repr(notifier) == "OutboxNotifier(delivered=1)"


def test_deliver_does_not_change_the_notification_state(notification):
    OutboxNotifier(log=None).deliver(notification)
    assert not notification.is_final   # marking it Sent is the directory's job


def test_nothing_to_deliver_without_recipients():
    site = Site(1, "x", "https://example.com")
    site.change_status(SiteStatus.DOWN, "x")
    empty = Notification.draft_for(site, SiteStatus.OPERATIONAL, [])
    with pytest.raises(NotificationDeliveryError, match="no recipients"):
        OutboxNotifier(log=None).deliver(empty)


def test_delivery_error_is_a_runtime_error():
    assert issubclass(NotificationDeliveryError, RuntimeError)
