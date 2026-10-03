"""
Notification delivery for the Sites & Mailing Lists subsystem.

    Notifier          abstract interface: deliver(notification)
    └── OutboxNotifier  records each delivery in the app (no real email)

SiteDirectory.send_notification() (app/sites.py) only talks to the Notifier
interface. Real email (e.g. an SmtpNotifier reading server settings from
.env) can be added later as one more subclass, with no change anywhere
else - the seed data uses synthetic @example.com addresses, so nothing real
could be delivered today anyway.

Contract for every Notifier: deliver() either delivers the whole
notification or raises NotificationDeliveryError. It never changes the
Notification's state itself - SiteDirectory marks it Sent only after
deliver() returns, so a failed delivery leaves a Draft that can be retried.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Callable, List, NamedTuple, Optional, Tuple

from app.models import Notification


class NotificationDeliveryError(RuntimeError):
    """
    Delivery failed. A RuntimeError subclass, so the API layer's existing
    RuntimeError handler already turns it into an HTTP error response.
    """


class DeliveredMessage(NamedTuple):
    """One message as handed to the recipients - what OutboxNotifier records."""
    notification_id: str
    recipients: Tuple[str, ...]
    subject: str
    body: str
    delivered_at: datetime


class Notifier(ABC):
    """Interface every notification sender implements - see the module docstring for the contract."""

    @staticmethod
    def subject_for(notification: Notification) -> str:
        """Shared subject line, e.g. '[Incident Bridge] Site 1042 (Customer portal) is now Down'."""
        status = notification.new_status.value.replace("_", " ").capitalize()
        return f"[Incident Bridge] Site {notification.site_id} ({notification.site_name}) is now {status}"

    @abstractmethod
    def deliver(self, notification: Notification) -> None:
        """Deliver `notification` to all its recipients, or raise NotificationDeliveryError."""

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"


class OutboxNotifier(Notifier):
    """
    Records every delivery in an in-memory outbox instead of sending email,
    and writes one log line per delivery (print by default). The Sent state
    itself is persisted with the notification (app/persistence_sites.py);
    the outbox is only this process's delivery log, so it starts empty after
    a restart.
    """

    def __init__(self, log: Optional[Callable[[str], None]] = print):
        self.outbox: List[DeliveredMessage] = []
        self._log = log

    def deliver(self, notification: Notification) -> None:
        if not notification.recipients:
            raise NotificationDeliveryError("This notification has no recipients.")
        message = DeliveredMessage(
            notification_id=notification.id,
            recipients=tuple(sorted(notification.recipients)),
            subject=self.subject_for(notification),
            body=notification.message,
            delivered_at=datetime.now(timezone.utc),
        )
        self.outbox.append(message)
        if self._log:
            self._log(f"Notification delivered (outbox): {message.subject} -> {len(message.recipients)} recipients")

    def __repr__(self) -> str:
        return f"OutboxNotifier(delivered={len(self.outbox)})"
